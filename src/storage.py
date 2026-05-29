"""SQLite persistence layer. All state lives in data/db/agent.db (no external DB).

Tables: signals (raw fetched items) → stories (deduped clusters) → digests
+ digest_stories (which stories went in which daily digest, with rank).

Stdlib sqlite3 only — no ORM. Every public function accepts an optional `conn`
so tests can inject a tmpfile connection; the default opens a fresh connection
to config.DB_PATH and closes it.
"""
from __future__ import annotations

import array
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

import config
from models import Signal, Story, signal_id


# --- Schema -------------------------------------------------------------

_SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS stories (
        id TEXT PRIMARY KEY,
        canonical_url TEXT NOT NULL,
        canonical_title TEXT NOT NULL,
        canonical_summary TEXT,
        published_at TEXT NOT NULL,
        relevance_score REAL NOT NULL,
        created_at TEXT NOT NULL,
        priority_bucket TEXT,
        geo TEXT,
        embedding BLOB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stories_score ON stories(relevance_score DESC)",
    # idx_stories_priority_bucket is created in _migrate() *after* the column
    # has been ensured to exist — keeping it here breaks startup on any DB that
    # predates the priority_bucket column (CREATE INDEX runs before ALTER TABLE).
    """
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_type TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        published_at TEXT NOT NULL,
        summary TEXT,
        raw_json TEXT,
        fetched_at TEXT NOT NULL,
        story_id TEXT REFERENCES stories(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_story_id   ON signals(story_id)",
    "CREATE INDEX IF NOT EXISTS idx_signals_fetched_at ON signals(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_url        ON signals(url)",
    """
    CREATE TABLE IF NOT EXISTS digests (
        id TEXT PRIMARY KEY,
        digest_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sent_at TEXT,
        status TEXT NOT NULL,
        recipients TEXT NOT NULL,
        error TEXT,
        slack_ts TEXT,
        slack_channel TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_digests_sent_at ON digests(sent_at)",
    # idx_digests_slack_ts is created in _migrate() *after* the column is added
    # so this works on pre-existing DBs that don't yet have slack_ts.
    """
    CREATE TABLE IF NOT EXISTS digest_stories (
        digest_id TEXT NOT NULL REFERENCES digests(id),
        story_id  TEXT NOT NULL REFERENCES stories(id),
        rank INTEGER NOT NULL,
        reasoning TEXT,
        domain TEXT,
        PRIMARY KEY (digest_id, story_id)
    )
    """,
    # Phase 2 feedback loop: events pulled from Vercel Blob receiver.
    """
    CREATE TABLE IF NOT EXISTS feedback (
        event_id TEXT PRIMARY KEY,
        received_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        slack_user TEXT,
        reaction TEXT,
        slack_ts TEXT,
        slack_channel TEXT,
        digest_id TEXT REFERENCES digests(id),
        raw_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_feedback_digest_id ON feedback(digest_id)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_slack_ts  ON feedback(slack_ts)",
    """
    CREATE TABLE IF NOT EXISTS feedback_cursor (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_received_at TEXT NOT NULL
    )
    """,
]


def _migrate(c: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE migrations for pre-existing databases."""
    cur = c.execute("PRAGMA table_info(digest_stories)")
    cols = {row["name"] for row in cur.fetchall()}
    if "domain" not in cols:
        c.execute("ALTER TABLE digest_stories ADD COLUMN domain TEXT")

    cur = c.execute("PRAGMA table_info(stories)")
    cols = {row["name"] for row in cur.fetchall()}
    if "priority_bucket" not in cols:
        c.execute("ALTER TABLE stories ADD COLUMN priority_bucket TEXT")
    if "geo" not in cols:
        c.execute("ALTER TABLE stories ADD COLUMN geo TEXT")
    if "embedding" not in cols:
        c.execute("ALTER TABLE stories ADD COLUMN embedding BLOB")

    cur = c.execute("PRAGMA table_info(digests)")
    cols = {row["name"] for row in cur.fetchall()}
    if "slack_ts" not in cols:
        c.execute("ALTER TABLE digests ADD COLUMN slack_ts TEXT")
    if "slack_channel" not in cols:
        c.execute("ALTER TABLE digests ADD COLUMN slack_channel TEXT")

    # Create indexes for migration-added columns AFTER the columns exist.
    # Idempotent so it's fine to also run on fresh DBs where CREATE TABLE
    # already produced the column.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_stories_priority_bucket "
        "ON stories(priority_bucket)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_digests_slack_ts ON digests(slack_ts)"
    )


# --- Connection management ---------------------------------------------

def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _maybe_own(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    own = connect()
    try:
        yield own
        own.commit()
    finally:
        own.close()


def init_db(*, conn: sqlite3.Connection | None = None) -> None:
    with _maybe_own(conn) as c:
        for stmt in _SCHEMA_SQL:
            c.execute(stmt)
        _migrate(c)
        if conn is not None:
            c.commit()


# --- Helpers ------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _signal_from_row(row: sqlite3.Row) -> Signal:
    raw = {}
    if row["raw_json"]:
        try:
            raw = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            raw = {}
    return Signal(
        source=row["source"],
        source_type=row["source_type"],
        title=row["title"],
        url=row["url"],
        published_at=_parse_iso(row["published_at"]),
        summary=row["summary"] or "",
        raw=raw,
    )


def _embedding_to_blob(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _blob_to_embedding(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def _story_from_row(row: sqlite3.Row) -> Story:
    keys = row.keys()
    pb = row["priority_bucket"] if "priority_bucket" in keys else None
    geo = row["geo"] if "geo" in keys else None
    return Story(
        id=row["id"],
        canonical_url=row["canonical_url"],
        canonical_title=row["canonical_title"],
        canonical_summary=row["canonical_summary"] or "",
        published_at=_parse_iso(row["published_at"]),
        relevance_score=float(row["relevance_score"]),
        signal_ids=(),  # populated by callers if they need it
        priority_bucket=pb,
        geo=geo,
    )


# --- Signals ------------------------------------------------------------

def save_signals(
    signals: Iterable[Signal],
    *,
    fetched_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    fetched_at = fetched_at or _utcnow()
    fetched_iso = _iso(fetched_at)
    rows = []
    for s in signals:
        sid = signal_id(s.source, s.url)
        rows.append((
            sid, s.source, s.source_type, s.title, s.url,
            _iso(s.published_at), s.summary,
            json.dumps(s.raw, default=str) if s.raw else None,
            fetched_iso,
        ))
    if not rows:
        return 0
    with _maybe_own(conn) as c:
        cur = c.executemany(
            """INSERT OR IGNORE INTO signals
               (id, source, source_type, title, url, published_at,
                summary, raw_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return cur.rowcount


def list_unscored_signals(*, conn: sqlite3.Connection | None = None) -> list[Signal]:
    with _maybe_own(conn) as c:
        rows = c.execute(
            "SELECT * FROM signals WHERE story_id IS NULL ORDER BY published_at DESC"
        ).fetchall()
    return [_signal_from_row(r) for r in rows]


def list_signals_since(
    since: datetime,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[Signal]:
    with _maybe_own(conn) as c:
        rows = c.execute(
            "SELECT * FROM signals WHERE fetched_at >= ? ORDER BY published_at DESC",
            (_iso(since),),
        ).fetchall()
    return [_signal_from_row(r) for r in rows]


# --- Stories ------------------------------------------------------------

def upsert_story(
    story: Story,
    *,
    embedding: list[float] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    emb_blob = _embedding_to_blob(embedding) if embedding else None
    with _maybe_own(conn) as c:
        c.execute(
            """INSERT INTO stories
               (id, canonical_url, canonical_title, canonical_summary,
                published_at, relevance_score, created_at, priority_bucket,
                geo, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 canonical_url     = excluded.canonical_url,
                 canonical_title   = excluded.canonical_title,
                 canonical_summary = excluded.canonical_summary,
                 published_at      = excluded.published_at,
                 relevance_score   = excluded.relevance_score,
                 priority_bucket   = excluded.priority_bucket,
                 geo               = excluded.geo,
                 embedding         = COALESCE(excluded.embedding, stories.embedding)""",
            (
                story.id, story.canonical_url, story.canonical_title,
                story.canonical_summary, _iso(story.published_at),
                story.relevance_score, _iso(_utcnow()),
                story.priority_bucket, story.geo, emb_blob,
            ),
        )


def assign_signal_to_story(
    signal_id_: str,
    story_id_: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _maybe_own(conn) as c:
        # SQLite enforces the FK on UPDATE (PRAGMA foreign_keys=ON is set in
        # connect()), so a bogus story_id raises sqlite3.IntegrityError without
        # any extra SELECT.
        c.execute(
            "UPDATE signals SET story_id = ? WHERE id = ?",
            (story_id_, signal_id_),
        )


def assign_signals_to_story(
    rows: Iterable[tuple[str, str]],
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Bulk version of assign_signal_to_story. `rows` is an iterable of
    (signal_id, story_id) pairs. One executemany roundtrip; FK still enforced
    on each row, so a bogus story_id anywhere in the batch raises IntegrityError.
    """
    pairs = [(sid_story, sid_signal) for sid_signal, sid_story in rows]
    if not pairs:
        return
    with _maybe_own(conn) as c:
        c.executemany(
            "UPDATE signals SET story_id = ? WHERE id = ?",
            pairs,
        )


def list_stories(
    *,
    min_score: float = 0.0,
    limit: int = 100,
    exclude_urls: Iterable[str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Story]:
    """Top stories by relevance_score.

    `exclude_urls` is the dedup escape hatch the ranker uses to keep already-
    sent stories out of its candidate pool — without it, high-scoring
    evergreens from previous digests outcompete today's fresh stories.
    """
    excluded = tuple(exclude_urls) if exclude_urls else ()
    params: list = [min_score]
    sql = "SELECT * FROM stories WHERE relevance_score >= ?"
    if excluded:
        placeholders = ",".join("?" * len(excluded))
        sql += f" AND canonical_url NOT IN ({placeholders})"
        params.extend(excluded)
    sql += " ORDER BY relevance_score DESC LIMIT ?"
    params.append(limit)
    with _maybe_own(conn) as c:
        rows = c.execute(sql, params).fetchall()
    return [_story_from_row(r) for r in rows]


# --- Digests ------------------------------------------------------------

def create_digest(
    digest_date: str,
    recipients: Iterable[str],
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    digest_id = str(uuid.uuid4())
    with _maybe_own(conn) as c:
        c.execute(
            """INSERT INTO digests
               (id, digest_date, created_at, sent_at, status, recipients, error)
               VALUES (?, ?, ?, NULL, 'pending', ?, NULL)""",
            (digest_id, digest_date, _iso(_utcnow()),
             json.dumps(list(recipients))),
        )
    return digest_id


def add_story_to_digest(
    digest_id: str,
    story_id_: str,
    rank: int,
    reasoning: str = "",
    domain: str = "",
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _maybe_own(conn) as c:
        c.execute(
            """INSERT INTO digest_stories (digest_id, story_id, rank, reasoning, domain)
               VALUES (?, ?, ?, ?, ?)""",
            (digest_id, story_id_, rank, reasoning, domain),
        )


def mark_digest_sent(
    digest_id: str,
    sent_at: datetime | None = None,
    *,
    slack_ts: str | None = None,
    slack_channel: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    sent = sent_at or _utcnow()
    with _maybe_own(conn) as c:
        c.execute(
            "UPDATE digests SET status='sent', sent_at=?, error=NULL, "
            "slack_ts=COALESCE(?, slack_ts), "
            "slack_channel=COALESCE(?, slack_channel) "
            "WHERE id=?",
            (_iso(sent), slack_ts, slack_channel, digest_id),
        )


def mark_digest_failed(
    digest_id: str,
    error: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _maybe_own(conn) as c:
        c.execute(
            "UPDATE digests SET status='failed', error=? WHERE id=?",
            (error, digest_id),
        )


# --- No-repeat checks (CLAUDE.md key constraint) -----------------------

def recently_sent_urls(
    within_days: int = 30,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> set[str]:
    cutoff = (now or _utcnow()) - timedelta(days=within_days)
    with _maybe_own(conn) as c:
        rows = c.execute(
            """SELECT DISTINCT s.canonical_url
               FROM digest_stories ds
               JOIN digests d ON d.id = ds.digest_id
               JOIN stories s ON s.id = ds.story_id
               WHERE d.status = 'sent' AND d.sent_at >= ?""",
            (_iso(cutoff),),
        ).fetchall()
    return {r["canonical_url"] for r in rows}


def recent_story_embeddings(
    within_days: int = 30,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> list[tuple[str, list[float]]]:
    """Embeddings of stories sent in any digest within the last N days.

    Returns (story_id, embedding) pairs. Stories with NULL embeddings (rows
    written before the embedding column existed, or written without one) are
    excluded — they simply can't participate in similarity checks.
    """
    cutoff = (now or _utcnow()) - timedelta(days=within_days)
    with _maybe_own(conn) as c:
        rows = c.execute(
            """SELECT DISTINCT s.id, s.embedding
               FROM digest_stories ds
               JOIN digests d ON d.id = ds.digest_id
               JOIN stories s ON s.id = ds.story_id
               WHERE d.status = 'sent'
                 AND d.sent_at >= ?
                 AND s.embedding IS NOT NULL""",
            (_iso(cutoff),),
        ).fetchall()
    return [(r["id"], _blob_to_embedding(r["embedding"])) for r in rows]
