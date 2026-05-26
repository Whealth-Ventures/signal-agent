"""Smoke tests for src/storage.py — every test gets a fresh tmpfile DB."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import storage  # noqa: E402
from models import Signal, Story, signal_id, story_id  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _DBTestBase(unittest.TestCase):
    """Each test gets its own tmpdir + DB connection."""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = storage.connect(self.db_path)
        storage.init_db(conn=self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()


class InitDbTest(_DBTestBase):
    def test_idempotent(self) -> None:
        storage.init_db(conn=self.conn)
        storage.init_db(conn=self.conn)
        # Schema sanity: all four tables exist
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertTrue({"signals", "stories", "digests", "digest_stories"}.issubset(names))


class SignalsRoundTripTest(_DBTestBase):
    def _make_signal(self, source="KFF", url="https://kff.example/a", title="Title A") -> Signal:
        return Signal(
            source=source, source_type="rss",
            title=title, url=url,
            published_at=_utcnow() - timedelta(hours=2),
            summary="A short summary.",
            raw={"k": "v"},
        )

    def test_save_and_list(self) -> None:
        sig = self._make_signal()
        n = storage.save_signals([sig], conn=self.conn)
        self.conn.commit()
        self.assertEqual(n, 1)

        loaded = storage.list_unscored_signals(conn=self.conn)
        self.assertEqual(len(loaded), 1)
        l = loaded[0]
        self.assertEqual(l.source, sig.source)
        self.assertEqual(l.url, sig.url)
        self.assertEqual(l.title, sig.title)
        self.assertEqual(l.summary, sig.summary)
        self.assertEqual(l.raw, {"k": "v"})

    def test_insert_or_ignore_dedupes(self) -> None:
        sig = self._make_signal()
        storage.save_signals([sig], conn=self.conn)
        storage.save_signals([sig], conn=self.conn)
        self.conn.commit()
        rows = self.conn.execute("SELECT count(*) AS c FROM signals").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_list_signals_since(self) -> None:
        old_sig = self._make_signal(url="https://x.example/old")
        new_sig = self._make_signal(url="https://x.example/new")
        storage.save_signals([old_sig], fetched_at=_utcnow() - timedelta(hours=10), conn=self.conn)
        storage.save_signals([new_sig], fetched_at=_utcnow() - timedelta(hours=1), conn=self.conn)
        self.conn.commit()
        recent = storage.list_signals_since(_utcnow() - timedelta(hours=2), conn=self.conn)
        urls = {s.url for s in recent}
        self.assertEqual(urls, {"https://x.example/new"})


class StoryUpsertTest(_DBTestBase):
    def _story(self, score: float = 0.5, summary="orig") -> Story:
        url = "https://x.example/canonical"
        return Story(
            id=story_id(url),
            canonical_url=url,
            canonical_title="Big news",
            canonical_summary=summary,
            published_at=_utcnow() - timedelta(hours=1),
            relevance_score=score,
        )

    def test_insert_then_update(self) -> None:
        s1 = self._story(score=0.5, summary="orig")
        storage.upsert_story(s1, conn=self.conn)
        self.conn.commit()

        s2 = self._story(score=0.9, summary="updated")
        storage.upsert_story(s2, conn=self.conn)
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT relevance_score, canonical_summary FROM stories WHERE id = ?",
            (s1.id,),
        ).fetchone()
        self.assertAlmostEqual(rows["relevance_score"], 0.9)
        self.assertEqual(rows["canonical_summary"], "updated")
        # Still only one row
        c = self.conn.execute("SELECT count(*) AS c FROM stories").fetchone()["c"]
        self.assertEqual(c, 1)


class AssignSignalTest(_DBTestBase):
    def test_assign_unscored_now_excluded(self) -> None:
        sig = Signal(
            source="x", source_type="rss",
            title="t", url="https://e.example/1",
            published_at=_utcnow(), summary="",
        )
        storage.save_signals([sig], conn=self.conn)
        st = Story(
            id=story_id("https://e.example/1"),
            canonical_url="https://e.example/1",
            canonical_title="t", canonical_summary="",
            published_at=_utcnow(), relevance_score=0.7,
        )
        storage.upsert_story(st, conn=self.conn)
        storage.assign_signal_to_story(signal_id("x", "https://e.example/1"), st.id, conn=self.conn)
        self.conn.commit()

        unscored = storage.list_unscored_signals(conn=self.conn)
        self.assertEqual(unscored, [])

    def test_assign_to_missing_story_raises(self) -> None:
        sig = Signal(
            source="x", source_type="rss",
            title="t", url="https://e.example/1",
            published_at=_utcnow(),
        )
        storage.save_signals([sig], conn=self.conn)
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            storage.assign_signal_to_story(
                signal_id("x", "https://e.example/1"),
                "nonexistent_story_id",
                conn=self.conn,
            )


class DigestLifecycleTest(_DBTestBase):
    def _setup_story(self, url: str, score: float = 0.8) -> str:
        st = Story(
            id=story_id(url),
            canonical_url=url,
            canonical_title=f"Title for {url}",
            canonical_summary="",
            published_at=_utcnow(),
            relevance_score=score,
        )
        storage.upsert_story(st, conn=self.conn)
        return st.id

    def test_create_add_send(self) -> None:
        sid = self._setup_story("https://e.example/a")
        did = storage.create_digest("2026-05-05", ["a@x.com", "b@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, sid, rank=1, reasoning="top story", conn=self.conn)
        storage.mark_digest_sent(did, conn=self.conn)
        self.conn.commit()

        row = self.conn.execute("SELECT * FROM digests WHERE id = ?", (did,)).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertIsNotNone(row["sent_at"])

        ds = self.conn.execute(
            "SELECT * FROM digest_stories WHERE digest_id = ?", (did,)
        ).fetchone()
        self.assertEqual(ds["rank"], 1)
        self.assertEqual(ds["reasoning"], "top story")

    def test_mark_failed_sets_error(self) -> None:
        did = storage.create_digest("2026-05-05", ["a@x.com"], conn=self.conn)
        storage.mark_digest_failed(did, "SMTP timeout", conn=self.conn)
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM digests WHERE id = ?", (did,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "SMTP timeout")


class StoryEmbeddingTest(_DBTestBase):
    def test_round_trip(self) -> None:
        vec = [0.1, -0.2, 0.5, 0.0, 1.0]
        url = "https://emb.example/a"
        st = Story(
            id=story_id(url), canonical_url=url, canonical_title="t",
            canonical_summary="", published_at=_utcnow(), relevance_score=0.5,
        )
        storage.upsert_story(st, embedding=vec, conn=self.conn)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT embedding FROM stories WHERE id = ?", (st.id,)
        ).fetchone()
        self.assertIsNotNone(row["embedding"])
        loaded = storage._blob_to_embedding(row["embedding"])
        self.assertEqual(len(loaded), len(vec))
        for a, b in zip(loaded, vec):
            self.assertAlmostEqual(a, b, places=5)

    def test_reupsert_without_embedding_preserves_existing(self) -> None:
        vec = [0.3, 0.7]
        url = "https://emb.example/preserve"
        st = Story(
            id=story_id(url), canonical_url=url, canonical_title="t",
            canonical_summary="", published_at=_utcnow(), relevance_score=0.5,
        )
        storage.upsert_story(st, embedding=vec, conn=self.conn)
        # Re-upsert with no embedding kwarg — must NOT blank the column.
        storage.upsert_story(st, conn=self.conn)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT embedding FROM stories WHERE id = ?", (st.id,)
        ).fetchone()
        self.assertIsNotNone(row["embedding"])


class RecentStoryEmbeddingsTest(_DBTestBase):
    def _make_sent(
        self,
        url: str,
        sent_at: datetime,
        embedding: list[float] | None,
    ) -> str:
        sid = story_id(url)
        storage.upsert_story(
            Story(id=sid, canonical_url=url, canonical_title="t",
                  canonical_summary="", published_at=_utcnow(),
                  relevance_score=0.5),
            embedding=embedding, conn=self.conn,
        )
        did = storage.create_digest("2026-05-05", ["a@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, sid, rank=1, conn=self.conn)
        storage.mark_digest_sent(did, sent_at, conn=self.conn)
        return sid

    def test_within_window_returned(self) -> None:
        sid = self._make_sent(
            "https://rs.example/a", _utcnow() - timedelta(days=3), [1.0, 0.0],
        )
        self.conn.commit()
        out = storage.recent_story_embeddings(within_days=30, conn=self.conn)
        ids = {s for s, _ in out}
        self.assertIn(sid, ids)

    def test_outside_window_excluded(self) -> None:
        self._make_sent(
            "https://rs.example/old", _utcnow() - timedelta(days=45), [0.0, 1.0],
        )
        self.conn.commit()
        out = storage.recent_story_embeddings(within_days=30, conn=self.conn)
        self.assertEqual(out, [])

    def test_null_embedding_excluded(self) -> None:
        self._make_sent(
            "https://rs.example/null", _utcnow() - timedelta(days=2), None,
        )
        self.conn.commit()
        out = storage.recent_story_embeddings(within_days=30, conn=self.conn)
        self.assertEqual(out, [])


class RecentlySentUrlsTest(_DBTestBase):
    def _setup(self, url: str, sent_at: datetime | None, status: str = "sent") -> None:
        sid = story_id(url)
        storage.upsert_story(
            Story(id=sid, canonical_url=url, canonical_title="t",
                  canonical_summary="", published_at=_utcnow(),
                  relevance_score=0.5),
            conn=self.conn,
        )
        did = storage.create_digest("2026-05-05", ["a@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, sid, rank=1, conn=self.conn)
        if status == "sent" and sent_at is not None:
            storage.mark_digest_sent(did, sent_at, conn=self.conn)
        elif status == "failed":
            storage.mark_digest_failed(did, "x", conn=self.conn)

    def test_within_window(self) -> None:
        self._setup("https://recent.example/a", _utcnow() - timedelta(days=3))
        self.conn.commit()
        urls = storage.recently_sent_urls(within_days=7, conn=self.conn)
        self.assertIn("https://recent.example/a", urls)

    def test_outside_window(self) -> None:
        self._setup("https://old.example/a", _utcnow() - timedelta(days=10))
        self.conn.commit()
        urls = storage.recently_sent_urls(within_days=7, conn=self.conn)
        self.assertNotIn("https://old.example/a", urls)

    def test_failed_digest_excluded(self) -> None:
        self._setup("https://failed.example/a", None, status="failed")
        self.conn.commit()
        urls = storage.recently_sent_urls(within_days=7, conn=self.conn)
        self.assertNotIn("https://failed.example/a", urls)

    def test_pending_digest_excluded(self) -> None:
        # _setup with sent_at=None and status='sent' falls through; create explicit pending
        sid = story_id("https://pending.example/a")
        storage.upsert_story(
            Story(id=sid, canonical_url="https://pending.example/a",
                  canonical_title="t", canonical_summary="",
                  published_at=_utcnow(), relevance_score=0.5),
            conn=self.conn,
        )
        did = storage.create_digest("2026-05-05", ["a@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, sid, rank=1, conn=self.conn)
        # Never marked sent → status='pending'
        self.conn.commit()
        urls = storage.recently_sent_urls(within_days=7, conn=self.conn)
        self.assertNotIn("https://pending.example/a", urls)


if __name__ == "__main__":
    unittest.main()
