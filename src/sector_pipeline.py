"""Sector-digest orchestrator (bi-weekly).

Per sector: build facet plans → Perplexity sweep (14-day window) → save →
score+cluster (stream-scoped, 14-day dedup) → magnitude-tier into event-type
groups → post one Slack message to the shared sector channel.

Runs all 7 sectors in one invocation (shared connection + Perplexity budget
scope), each an independent stream so dedup and idempotency don't collide. RSS
is intentionally skipped — sector signal comes from the targeted Perplexity
facet sweeps, not the general newsletter list.

Invoked via `python src/main.py --mode sector` (all sectors) or
`--mode sector --sector <key>` (one). Imported lazily by main.py to avoid an
import cycle.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import main as daily_main
import scorer
import sector_ranker
import sector_slack
import storage
from perplexity_client import PerplexityClient
from query_planner import Sector, build_sector_plans, get_sector, load_sectors
from ranker import _build_ranker_client

SECTOR_BUDGET_SCOPE = "sector"


@dataclass(frozen=True)
class SectorRunResult:
    sector_key: str
    display: str
    posted: bool          # delivered (Slack sent, or ranked+recorded for PDF)
    items: int
    skipped_already_sent: bool
    error: str | None = None
    # Carried for PDF assembly (deliver="pdf"): the ranking to render and the
    # pending digest row to mark sent once the PDF is written.
    ranking: "sector_ranker.SectorRankingResult | None" = None
    sector: "Sector | None" = None
    digest_id: str | None = None


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    path = config.LOGS_DIR / f"sector_pipeline_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _date_label(now: datetime, days: int = 14) -> str:
    """e.g. 'Jul 9–22, 2026' for a 14-day window ending today."""
    end = now
    start = now - timedelta(days=days - 1)
    if start.year == end.year:
        return f"{start:%b} {start.day}–{end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year}–{end:%b} {end.day}, {end.year}"


def run_one_sector(
    sector: Sector,
    *,
    conn: sqlite3.Connection,
    perplexity_client: PerplexityClient,
    ranker_client,
    chroma_client=None,
    embedder=None,
    http_client=None,
    digest_date: str,
    date_label: str,
    deliver: str = "slack",
    skip_url_validation: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
    force: bool = False,
    max_plans: int | None = None,
    now: datetime | None = None,
) -> SectorRunResult:
    now = now or datetime.now(timezone.utc)
    stream = f"sector:{sector.key}"
    t0 = time.monotonic()
    _progress(f"\n── Sector: {sector.display} ({sector.portco}) ──")

    # 1) Fetch (Perplexity facet sweeps only).
    plans = build_sector_plans(sector)
    if max_plans is not None:
        plans = plans[:max_plans]
    _progress(f"  [1/4] Perplexity: {len(plans)} facet plans "
              f"({perplexity_client.remaining_today} calls left in scope)")
    perp_signals = asyncio.run(
        daily_main.fetch_perplexity_async(perplexity_client, plans)
    )
    storage.save_signals(perp_signals, conn=conn)
    conn.commit()
    _progress(f"       {len(perp_signals)} signals")

    # 2) Score + cluster, scoped to this sector's stream + a 14-day dedup window.
    _progress("  [2/4] Scoring + dedupe (stream-scoped)…")
    scorer.run_scoring(
        conn=conn, chroma_client=chroma_client, embedder=embedder,
        url_dedup_window_days=sector_ranker.SECTOR_DEDUP_WINDOW_DAYS,
        historical_dedup_window_days=sector_ranker.SECTOR_DEDUP_WINDOW_DAYS,
        stream=stream,
    )
    conn.commit()

    # 3) Rank into event-type groups.
    _progress("  [3/4] Ranking (event-type tiering)…")
    result = sector_ranker.rank_sector(
        sector, conn=conn, client=ranker_client, now=now,
    )
    _progress(f"       {result.total} items across {len(result.groups)} groups "
              f"(fallback={result.used_fallback}, ${result.cost_usd:.4f})")

    # 4) Deliver.
    if dry_run:
        blocks = sector_slack.build_sector_blocks(
            result, sector, date_label=date_label,
        )
        out = config.LOGS_DIR / f"dry_run_sector_{sector.key}_{digest_date}.json"
        out.write_text(
            json.dumps({"text": f"{sector.display} — sector roundup",
                        "blocks": blocks}, indent=2),
            encoding="utf-8",
        )
        _progress(f"  [4/4] Dry-run: wrote {out.name} (no Slack post).")
        _log({"sector": sector.key, "dry_run": True, "items": result.total})
        return SectorRunResult(sector.key, sector.display, False, result.total, False)

    if not force and not test_mode and storage.has_sent_digest_for_date(
        digest_date, stream=stream, conn=conn,
    ):
        _progress(f"  [4/4] Already delivered for {digest_date} — skipping "
                  f"(idempotent; --force to redo).")
        _log({"sector": sector.key, "skipped_already_sent": True})
        return SectorRunResult(
            sector.key, sector.display, True, result.total, True,
            ranking=result, sector=sector,
        )

    # Record a pending digest row (unless a throwaway test run) so dedup works
    # across fortnights regardless of delivery channel.
    digest_id = None
    if not test_mode:
        digest_id = storage.create_digest(
            digest_date, (config.SLACK_CHANNEL_LABEL_SECTOR,),
            slack_channel=config.SLACK_CHANNEL_ID_SECTOR or None,
            stream=stream, conn=conn,
        )
        rank_idx = 0
        seen: set[str] = set()
        for display, items in result.groups:
            for b in items:
                rank_idx += 1
                # Record EVERY story a bullet covers (roll-ups merge several)
                # so all of them enter the fortnightly dedup window.
                for mid in (b.member_story_ids or (b.story.id,)):
                    if mid in seen:
                        continue
                    seen.add(mid)
                    storage.add_story_to_digest(
                        digest_id, mid, rank_idx, b.one_liner, display, conn=conn,
                    )
        conn.commit()

    if deliver == "pdf":
        # No delivery here — the combined PDF is rendered once, after every
        # sector is ranked. Leave the digest row pending; the pipeline marks it
        # sent after the PDF is written. Carry the ranking + digest_id up.
        _progress(f"  [4/4] Ranked for PDF ({result.total} items) "
                  f"({time.monotonic() - t0:.1f}s)")
        return SectorRunResult(
            sector.key, sector.display, True, result.total, False,
            ranking=result, sector=sector, digest_id=digest_id,
        )

    _progress("  [4/4] Posting to Slack…")
    slack_result = sector_slack.post_sector_digest(
        result, sector, digest_date=digest_date, date_label=date_label,
        channel_id=config.SLACK_CHANNEL_ID_SECTOR or None,
        http=http_client, skip_url_validation=skip_url_validation,
        test_mode=test_mode,
    )
    if digest_id is not None:
        if slack_result.sent:
            storage.mark_digest_sent(
                digest_id, slack_ts=slack_result.slack_ts,
                slack_channel=slack_result.slack_channel, conn=conn,
            )
        else:
            storage.mark_digest_failed(
                digest_id, slack_result.error or "unknown", conn=conn,
            )
        conn.commit()
    _progress(f"       sent={slack_result.sent} items={slack_result.stories_sent} "
              f"dropped={slack_result.stories_dropped_invalid_url} "
              f"({time.monotonic() - t0:.1f}s)")
    _log({
        "sector": sector.key, "sent": slack_result.sent,
        "items": slack_result.stories_sent,
        "dropped": slack_result.stories_dropped_invalid_url,
        "error": slack_result.error, "status": slack_result.status_code,
    })
    return SectorRunResult(
        sector.key, sector.display, slack_result.sent, slack_result.stories_sent,
        False, slack_result.error, ranking=result, sector=sector,
    )


def run_sector_pipeline(
    *,
    sector_key: str | None = None,
    deliver: str = "slack",
    pdf_out: str | None = None,
    min_days_between: int = 0,
    skip_url_validation: bool = False,
    skip_content_indexing: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
    force: bool = False,
    max_plans: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[SectorRunResult]:
    now = datetime.now(timezone.utc)
    digest_date = now.strftime("%Y-%m-%d")
    date_label = _date_label(now)

    # Bi-weekly guard: the systemd timer fires every Tuesday, but a fortnightly
    # digest should only actually run every ~2 weeks. Skip if a sector digest was
    # sent within `min_days_between` days. Bypassed by --force / --test / --dry-run
    # and for a single-sector manual run. Self-heals a missed cycle (next Tuesday
    # the gap has grown past the threshold, so it runs).
    if min_days_between > 0 and not (force or test_mode or dry_run):
        last = storage.most_recent_sent_at(stream_like="sector:%")
        if last is not None:
            days = (now - last).total_seconds() / 86400.0
            if days < min_days_between:
                _progress(
                    f"Bi-weekly guard: last sector digest was {days:.1f}d ago "
                    f"(< {min_days_between}d) — skipping this run. "
                    f"(--force to override.)"
                )
                _log({"skipped_biweekly_guard": True, "days_since_last": round(days, 2),
                      "min_days_between": min_days_between})
                return []

    sectors: list[Sector]
    if sector_key:
        s = get_sector(sector_key)
        if not s:
            raise SystemExit(f"Unknown sector '{sector_key}'. Known: "
                             f"{', '.join(x.key for x in load_sectors())}")
        sectors = [s]
    else:
        sectors = load_sectors()
    if not sectors:
        raise SystemExit(
            f"No sectors defined. Run `python scripts/build_sectors_xlsx.py` "
            f"to bootstrap {config.SECTORS_XLSX}."
        )

    own_conn = conn is None
    if own_conn:
        conn = storage.connect()
    storage.init_db(conn=conn)
    conn.commit()

    if not skip_content_indexing and not dry_run:
        _progress("[0] Content corpus check…")
        daily_main.ensure_content_indexed()

    # One Perplexity client for all sectors → shared 'sector' daily budget scope.
    perplexity_client = PerplexityClient(scope=SECTOR_BUDGET_SCOPE)
    ranker_client, _model = _build_ranker_client()

    _progress(f"Sector digest run · {date_label} · {len(sectors)} sector(s) "
              f"· deliver={deliver}")
    results: list[SectorRunResult] = []
    try:
        for sector in sectors:
            try:
                results.append(run_one_sector(
                    sector, conn=conn, perplexity_client=perplexity_client,
                    ranker_client=ranker_client, digest_date=digest_date,
                    date_label=date_label, deliver=deliver,
                    skip_url_validation=skip_url_validation,
                    dry_run=dry_run, test_mode=test_mode, force=force,
                    max_plans=max_plans, now=now,
                ))
            except Exception as e:  # one sector failing must not sink the rest
                _progress(f"  !! {sector.key} FAILED: {type(e).__name__}: {e}")
                _log({"sector": sector.key, "error": f"{type(e).__name__}: {e}"})
                results.append(SectorRunResult(
                    sector.key, sector.display, False, 0, False,
                    f"{type(e).__name__}: {e}",
                ))

        # PDF: render ONE combined document from every ranked sector, then mark
        # the pending digest rows sent (so the fortnightly dedup persists).
        if deliver == "pdf" and not dry_run:
            entries = [
                (r.ranking, r.sector) for r in results
                if r.ranking is not None and r.sector is not None
                and not r.skipped_already_sent
            ]
            if entries:
                import sector_pdf
                out = Path(pdf_out) if pdf_out else (
                    config.DATA_DIR / "exports"
                    / f"sector_signal_{digest_date}.pdf"
                )
                out = sector_pdf.render_sector_pdf(
                    entries, out, date_label=date_label,
                )
                for r in results:
                    if r.digest_id and not test_mode:
                        storage.mark_digest_sent(r.digest_id, conn=conn)
                conn.commit()
                _progress(f"\nPDF written: {out}")
                _log({"pdf_out": str(out), "sectors": len(entries)})
            else:
                _progress("\nNo sectors produced content — no PDF written.")
    finally:
        if own_conn:
            conn.close()

    done = sum(1 for r in results if r.posted)
    verb = "rendered" if deliver == "pdf" else "posted"
    _progress(f"\nSector digest done: {done}/{len(results)} {verb}.")
    return results
