"""src/neon_sync.py — one-way copy of the SQLite state into Neon Postgres, so the
archive can be browsed live (DBeaver, dashboards) from outside the box.

SQLite stays the source of truth: nothing reads Neon back, and a failed sync
only prints a WARN (deploy/run-digest.sh, deploy/run-sector.sh). The prod DB
only changes during runs, so a sync at the end of each run keeps Neon current.

  agent.db  → schema public
  sector.db → schema sector

Not copied: stories.embedding and signals.raw_json. They're binary / raw
payloads, unreadable in a SQL client, and most of the SQLite file's size.

What each run sends, per table:
  - empty in Neon (first sync, new table): every row
  - stories, signals: rows first seen in the last RESEND_DAYS; older rows
    never change (no deletes, and re-seen stories fall in the window)
  - digests, digest_stories: every row (small)
  - story_details: rows fetched or tagged after Neon's newest, so a missed
    sync catches up on its own
The upsert skips rows that haven't changed, so a re-sent row costs no write.

Bodies older than BODY_RETENTION_DAYS are nulled in Neon (row and tags stay)
so the free tier's 0.5 GB lasts ~4 years. SQLite keeps every body.

Needs DATABASE_URL: Neon's direct (non-pooled) connection string. Without it
the sync is skipped, not failed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

import config
import storage

RESEND_DAYS = 45
BODY_RETENTION_DAYS = 365

# table → (primary key, [(column, postgres type)], column that dates a row)
_TABLES: dict[str, tuple[tuple[str, ...], list[tuple[str, str]], str | None]] = {
    "stories": (("id",), [
        ("id", "text"), ("canonical_url", "text"), ("canonical_title", "text"),
        ("canonical_summary", "text"), ("published_at", "timestamptz"),
        ("relevance_score", "double precision"), ("created_at", "timestamptz"),
        ("priority_bucket", "text"), ("geo", "text"), ("bucket", "text"),
    ], "created_at"),
    "signals": (("id",), [
        ("id", "text"), ("source", "text"), ("source_type", "text"), ("title", "text"),
        ("url", "text"), ("published_at", "timestamptz"), ("summary", "text"),
        ("fetched_at", "timestamptz"), ("story_id", "text"),
    ], "fetched_at"),
    "digests": (("id",), [
        ("id", "text"), ("digest_date", "date"), ("created_at", "timestamptz"),
        ("sent_at", "timestamptz"), ("status", "text"), ("recipients", "text"),
        ("error", "text"), ("slack_ts", "text"), ("slack_channel", "text"),
    ], None),
    "digest_stories": (("digest_id", "story_id"), [
        ("digest_id", "text"), ("story_id", "text"), ("rank", "integer"),
        ("reasoning", "text"), ("domain", "text"),
    ], None),
    "story_details": (("story_id",), [
        ("story_id", "text"), ("body", "text"), ("body_status", "text"),
        ("fetched_at", "timestamptz"), ("category", "text"), ("facts", "jsonb"),
        ("magnitude", "text"), ("companies", "jsonb"), ("geo", "text"),
        ("summary", "text"), ("tagged_at", "timestamptz"), ("tag_model", "text"),
    ], None),  # windowed by Neon's own watermark instead, see _source_query
}


def _ddl(schema: str) -> list[str]:
    out = [f"CREATE SCHEMA IF NOT EXISTS {schema}"]
    for table, (pk, cols, _) in _TABLES.items():
        defs = ", ".join(f"{c} {t}" for c, t in cols)
        out.append(
            f"CREATE TABLE IF NOT EXISTS {schema}.{table} ({defs}, PRIMARY KEY ({', '.join(pk)}))"
        )
    return out


def _source_query(
    table: str, *, full: bool, watermark: str | None, now: datetime,
) -> tuple[str, dict]:
    """SQLite SELECT for the rows this run sends. `watermark` (story_details
    only) is Neon's newest fetched/tagged time, as a storage._iso string."""
    _, cols, dated_by = _TABLES[table]
    sql = f"SELECT {', '.join(c for c, _ in cols)} FROM {table}"
    params: dict = {}
    if full:
        return sql, params
    if table == "story_details" and watermark:
        sql += " WHERE fetched_at > :wm OR tagged_at > :wm"
        params["wm"] = watermark
    elif dated_by:
        sql += f" WHERE {dated_by} >= :since"
        params["since"] = storage._iso(now - timedelta(days=RESEND_DAYS))
    return sql, params


def _upsert_sql(schema: str, table: str) -> str:
    pk, cols, _ = _TABLES[table]
    names = [c for c, _ in cols]
    rest = [c for c in names if c not in pk]
    return (
        f"INSERT INTO {schema}.{table} AS t ({', '.join(names)}) "
        f"SELECT {', '.join(names)} FROM _stage_{table} "
        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in rest)
        # Unchanged row → no write, so re-sending the window costs nothing.
        + f" WHERE ({', '.join('t.' + c for c in rest)})"
        f" IS DISTINCT FROM ({', '.join('EXCLUDED.' + c for c in rest)})"
    )


def _upsert(cur, schema: str, table: str, rows: list[tuple]) -> None:
    """COPY into a temp stage (fast), then one upsert from it. Postgres parses
    SQLite's ISO timestamp and JSON text into timestamptz / jsonb on COPY.

    NUL bytes are stripped: SQLite stores them, Postgres text rejects them, and
    scraped pages do contain them (the first real sync failed on one)."""
    names = ", ".join(c for c, _ in _TABLES[table][1])
    cur.execute(f"CREATE TEMP TABLE _stage_{table} (LIKE {schema}.{table}) ON COMMIT DROP")
    with cur.copy(f"COPY _stage_{table} ({names}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
    cur.execute(_upsert_sql(schema, table))


def sync(pg, *, now: datetime | None = None) -> dict[str, int]:
    """Copy both SQLite DBs into `pg` (autocommit connection). One transaction
    per schema: a failure leaves that schema exactly as the last good sync."""
    now = now or datetime.now(timezone.utc)
    sent: dict[str, int] = {}
    for schema, path in (("public", config.DB_PATH), ("sector", config.SECTOR_DB_PATH)):
        if not Path(path).exists():
            continue
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            with pg.transaction(), pg.cursor() as cur:
                for stmt in _ddl(schema):
                    cur.execute(stmt)
                for table in _TABLES:
                    if table not in present:  # e.g. sector.db before its first post-deploy run
                        continue
                    full = cur.execute(
                        f"SELECT NOT EXISTS (SELECT 1 FROM {schema}.{table})"
                    ).fetchone()[0]
                    wm = None
                    if table == "story_details" and not full:
                        newest = cur.execute(
                            f"SELECT max(greatest(fetched_at, tagged_at)) FROM {schema}.story_details"
                        ).fetchone()[0]
                        # An hour of overlap: re-sent rows are free (see _upsert_sql).
                        wm = storage._iso(newest - timedelta(hours=1)) if newest else None
                    sql, params = _source_query(table, full=full, watermark=wm, now=now)
                    rows = src.execute(sql, params).fetchall()
                    if rows:
                        _upsert(cur, schema, table, rows)
                    sent[f"{schema}.{table}"] = len(rows)
                cur.execute(
                    f"UPDATE {schema}.story_details SET body = NULL "
                    "WHERE body IS NOT NULL AND fetched_at < %s",
                    (now - timedelta(days=BODY_RETENTION_DAYS),),
                )
        finally:
            src.close()
    return sent


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with (config.LOGS_DIR / f"neon_{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    if not config.DATABASE_URL:
        print("neon sync skipped: DATABASE_URL is not set")
        return 0
    t0 = time.monotonic()
    with psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=20) as pg:
        sent = sync(pg)
    secs = round(time.monotonic() - t0, 1)
    _log({"event": "sync_done", "sent": sent, "seconds": secs})
    print(f"neon sync: {json.dumps(sent)} in {secs}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
