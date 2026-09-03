"""One-shot backfill of `stories.geo` for RSS stories ingested before geo stamping.

WHY THIS EXISTS
---------------
`stories.geo` is set once, at story-creation time, by `scorer.pick_geo` from
the contributing signals' `raw["geo"]`. Until 2026-09-02 the RSS fetcher never
put a geo on its signals, so every RSS-only story was created with `geo=NULL`.

NULL is not neutral downstream:
  - `slack_client._GEO_TAG` renders it as `[GLOBAL]`
  - `ranker.filter_by_geo` keeps it for BOTH the India and US channels

The candidate pool is `dedup_window_days` deep (30), so those rows stay
eligible for weeks. On 2026-09-03 that was 1,186 NULL-geo stories, 43% of the
pool: six of that morning's sixteen digest stories were legacy NULL rows,
including Gland Pharma (India) and Sanford/North Memorial (US) both shipping
to the India channel as `[GLOBAL]`.

Every one of those rows is recoverable, because the signal rows kept their
`source` name and voices.xlsx maps each publication to a Geography. This script
applies exactly the mapping the live fetcher now applies, so a backfilled row
is indistinguishable from a freshly-stamped one.

It is a one-shot: once the pre-2026-09-02 rows age out of the window, it has
nothing left to do.

Usage:
    python scripts/backfill_story_geo.py                  # DRY RUN, prints only
    python scripts/backfill_story_geo.py --apply          # writes, after a snapshot
    python scripts/backfill_story_geo.py --apply --days 30
    python scripts/backfill_story_geo.py --revert data/logs/geo_backfill_<ts>.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
from query_planner import load_newsletters, load_voices  # noqa: E402
from rss_fetcher import _norm_signal_geo  # noqa: E402


def _source_geo_map() -> dict[str, str]:
    """{publication name → 'India' | 'US' | 'Global'} from voices.xlsx.

    Same two sheets the live fetcher draws on: Newsletters & Publications, and
    the Top Voices tabs for voices that have an rss_url.
    """
    out: dict[str, str] = {}
    for nl in load_newsletters():
        if nl.name and nl.geography:
            out[nl.name] = _norm_signal_geo(nl.geography)
    for v in load_voices():
        if v.name and v.geography and v.name not in out:
            out[v.name] = _norm_signal_geo(v.geography)
    return out


def _plan(conn: sqlite3.Connection, days: int) -> tuple[list[tuple[str, str]], Counter]:
    """Return ([(story_id, new_geo)], stats) without writing anything."""
    source_geo = _source_geo_map()
    stats: Counter = Counter()

    rows = conn.execute(
        """
        SELECT s.id AS story_id, sg.source AS source, sg.source_type AS source_type
        FROM stories s
        JOIN signals sg ON sg.story_id = s.id
        WHERE s.geo IS NULL
          AND s.created_at >= date('now', ?)
        """,
        (f"-{days} day",),
    ).fetchall()

    # Majority vote per story, mirroring scorer.pick_geo: most common geo among
    # contributing signals, first-seen order breaking a tie.
    per_story: dict[str, list[str]] = {}
    for r in rows:
        if r["source_type"] != "rss":
            stats["skipped_non_rss_signal"] += 1
            continue
        geo = source_geo.get(r["source"])
        if not geo:
            stats["skipped_unknown_source"] += 1
            stats[f"unknown::{r['source']}"] += 1
            continue
        per_story.setdefault(r["story_id"], []).append(geo)

    updates: list[tuple[str, str]] = []
    for sid, geos in per_story.items():
        counts = Counter(geos)
        top = max(counts, key=lambda g: (counts[g], -geos.index(g)))
        updates.append((sid, top))
        stats[f"to_{top}"] += 1

    stats["stories_null_before"] = len({r["story_id"] for r in rows})
    stats["stories_to_update"] = len(updates)
    stats["stories_left_null"] = stats["stories_null_before"] - len(updates)
    return updates, stats


def _snapshot(updates: list[tuple[str, str]]) -> Path:
    """Write the pre-change state so --revert can undo this exactly."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.LOGS_DIR / f"geo_backfill_{ts}.json"
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "created_at": ts,
                "note": "stories.geo was NULL for every id listed here",
                "ids": [sid for sid, _ in updates],
                "applied": [{"id": sid, "geo": geo} for sid, geo in updates],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Actually write. Without this, prints the plan and exits.")
    p.add_argument("--days", type=int, default=30,
                   help="How far back to backfill (default 30, the dedup window).")
    p.add_argument("--revert", metavar="SNAPSHOT_JSON",
                   help="Undo a previous --apply: set those ids back to NULL.")
    args = p.parse_args(argv)

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.revert:
        data = json.loads(Path(args.revert).read_text(encoding="utf-8"))
        applied = data.get("applied") or [{"id": i, "geo": None} for i in data["ids"]]
        # Narrow the revert the same way the apply was narrowed: only blank a
        # row that still holds exactly the value this snapshot wrote. A later
        # run may have re-derived a real geo for it, and blanking that would
        # destroy good data rather than undo us.
        cur = conn.executemany(
            "UPDATE stories SET geo = NULL WHERE id = ? AND geo = ?",
            [(row["id"], row["geo"]) for row in applied],
        )
        conn.commit()
        print(f"reverted {cur.rowcount} of {len(applied)} stories to geo=NULL")
        print("(rows whose geo has since changed were left alone)")
        return 0

    updates, stats = _plan(conn, args.days)

    print(f"NULL-geo stories in the last {args.days} days: "
          f"{stats['stories_null_before']}")
    print(f"  → would set India:  {stats.get('to_India', 0)}")
    print(f"  → would set US:     {stats.get('to_US', 0)}")
    print(f"  → would set Global: {stats.get('to_Global', 0)}")
    print(f"  → left NULL:        {stats['stories_left_null']}")
    unknown = {k.split("::", 1)[1]: v for k, v in stats.items()
               if k.startswith("unknown::")}
    if unknown:
        print("  sources not in voices.xlsx (left NULL):")
        for name, n in sorted(unknown.items(), key=lambda kv: -kv[1])[:15]:
            print(f"      {n:>5}  {name}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    if not updates:
        print("\nnothing to do")
        return 0

    snap = _snapshot(updates)
    print(f"\nsnapshot written: {snap}")
    conn.executemany(
        "UPDATE stories SET geo = ? WHERE id = ? AND geo IS NULL",
        [(geo, sid) for sid, geo in updates],
    )
    conn.commit()
    after = conn.execute(
        "SELECT COALESCE(geo,'NULL') g, count(*) n FROM stories "
        "WHERE created_at >= date('now', ?) GROUP BY 1 ORDER BY n DESC",
        (f"-{args.days} day",),
    ).fetchall()
    print(f"applied {len(updates)} updates. pool now:")
    for r in after:
        print(f"      {r['n']:>5}  {r['g']}")
    print(f"\nto undo: python scripts/backfill_story_geo.py --revert {snap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
