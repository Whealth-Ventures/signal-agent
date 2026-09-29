"""Tests for src/neon_sync.py's SQLite side and SQL generation. The Postgres
side is exercised by the live sync, not here: CI has no database."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        self.assertEqual(self._ids("stories", full=False, watermark=None), ["new"])
        wm = iso(NOW - timedelta(days=2))
        self.assertEqual(self._ids("story_details", full=False, watermark=wm), ["new"])

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
