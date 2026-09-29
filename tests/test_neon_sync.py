"""Tests for src/neon_sync.py's SQLite side and SQL generation. The Postgres
side is exercised by the live sync, not here: CI has no database."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import neon_sync  # noqa: E402
import storage  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
iso = storage._iso


class SourceQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = storage.connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.conn.close)
        storage.init_db(conn=self.conn)
        for sid, age in (("new", 1), ("old", 90)):
            t = iso(NOW - timedelta(days=age))
            self.conn.execute(
                "INSERT INTO stories (id, canonical_url, canonical_title, published_at, "
                "relevance_score, created_at, embedding) VALUES (?, ?, ?, ?, 0.5, ?, x'00')",
                (sid, f"https://ex.com/{sid}", sid, t, t),
            )
            self.conn.execute(
                "INSERT INTO story_details (story_id, body, body_status, fetched_at) VALUES (?, 'b', 'ok', ?)",
                (sid, t),
            )
        self.conn.commit()

    def _ids(self, table, **kw):
        sql, params = neon_sync._source_query(table, now=NOW, **kw)
        return sorted(r[0] for r in self.conn.execute(sql, params))

    def test_first_sync_sends_everything(self):
        self.assertEqual(self._ids("stories", full=True, watermark=None), ["new", "old"])
        self.assertEqual(self._ids("story_details", full=True, watermark=None), ["new", "old"])

    def test_later_syncs_send_only_the_window(self):
        wm = iso(NOW - timedelta(days=2))
        self.assertEqual(self._ids("story_details", full=False, watermark=wm), ["new"])
        sql, params = neon_sync._source_query("signals", full=False, watermark=None, now=NOW)
        self.assertIn("fetched_at >= :since", sql)
        self.assertEqual(params["since"], iso(NOW - timedelta(days=neon_sync.RESEND_DAYS)))

    def test_an_old_story_changed_in_place_is_always_re_sent(self):
        # A 90-day-old story re-seen today: upsert_story rewrites its score but
        # keeps created_at AND the article's own published_at, so no date
        # window would catch it. Stories are sent in full.
        self.conn.execute("UPDATE stories SET relevance_score = 0.91 WHERE id = 'old'")
        self.assertEqual(self._ids("stories", full=False, watermark=None), ["new", "old"])

    def test_a_late_tag_is_sent_even_for_an_old_fetch(self):
        self.conn.execute("UPDATE story_details SET tagged_at = ? WHERE story_id = 'old'", (iso(NOW),))
        wm = iso(NOW - timedelta(days=2))
        self.assertEqual(self._ids("story_details", full=False, watermark=wm), ["new", "old"])

    def test_embeddings_and_raw_payloads_are_never_selected(self):
        cols = {c for _, cs, _ in neon_sync._TABLES.values() for c, _ in cs}
        self.assertFalse({"embedding", "raw_json"} & cols)
        # Every mirrored column exists in the SQLite schema (catches a rename).
        for table, (_, cs, _) in neon_sync._TABLES.items():
            sqlite_cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertLessEqual({c for c, _ in cs}, sqlite_cols, table)


class _FakeCursor:
    """Just enough of a psycopg cursor for sync(): Neon's tables are non-empty
    and its newest story_details row is `newest`; COPY rows are captured."""
    def __init__(self, newest):
        self.newest, self.copied, self.executed, self._last = newest, {}, [], ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = sql
        return self

    def fetchone(self):
        if "NOT EXISTS" in self._last:
            return (False,)
        return (self.newest,)

    @contextmanager
    def copy(self, sql):
        rows = self.copied.setdefault(sql.split()[1].removeprefix("_stage_"), [])
        yield SimpleNamespace(write_row=rows.append)


class SyncTest(unittest.TestCase):
    setUp = SourceQueryTest.setUp  # same fixture, without re-running its tests

    def test_watermark_nul_strip_and_body_retention(self):
        self.conn.execute("UPDATE story_details SET body = 'a' || char(0) || 'b' WHERE story_id = 'new'")
        self.conn.commit()
        cur = _FakeCursor(newest=NOW - timedelta(days=2))
        pg = SimpleNamespace(transaction=nullcontext, cursor=lambda: cur)
        with mock.patch.object(config, "DB_PATH", Path(self.tmp.name) / "t.db"), \
             mock.patch.object(config, "SECTOR_DB_PATH", Path(self.tmp.name) / "missing.db"):
            sent = neon_sync.sync(pg, now=NOW)

        # Watermark: only the story_details row newer than Neon's newest is sent.
        self.assertEqual(sent["public.story_details"], 1)
        body = cur.copied["story_details"][0][1]
        self.assertEqual(body, "ab")  # NUL stripped on the way into COPY
        retention = [p for s, p in cur.executed if s.startswith("UPDATE public.story_details")]
        self.assertEqual(retention, [(NOW - timedelta(days=neon_sync.BODY_RETENTION_DAYS),)])
        self.assertNotIn("sector.stories", sent)  # no sector.db → schema skipped


class SqlTest(unittest.TestCase):
    def test_upsert_skips_unchanged_rows(self):
        sql = neon_sync._upsert_sql("public", "digest_stories")
        self.assertIn("ON CONFLICT (digest_id, story_id) DO UPDATE", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertNotIn("t.digest_id", sql)  # keys aren't in the change check

    def test_no_database_url_skips_without_connecting(self):
        with mock.patch.object(config, "DATABASE_URL", ""), \
             mock.patch.object(neon_sync.psycopg, "connect") as connect:
            self.assertEqual(neon_sync.main(), 0)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
