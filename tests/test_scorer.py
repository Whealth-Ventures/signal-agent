"""Smoke tests for src/scorer.py — pure-function coverage + end-to-end run."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import chromadb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import scorer  # noqa: E402
import storage  # noqa: E402
from content_indexer import ScoredChunk  # noqa: E402
from models import Signal, story_id  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ControlledEmbedder:
    """Returns the vector mapping[text]; raises if unmapped (catches typos)."""
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.calls = 0

    def __call__(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls += 1
        out = []
        for t in texts:
            if t not in self.mapping:
                raise KeyError(f"controlled embedder: no mapping for {t!r}")
            out.append(self.mapping[t])
        return out, max(1, sum(len(t) for t in texts) // 4)


# --- Pure function tests ------------------------------------------------

class BoostersTest(unittest.TestCase):
    def _sig(self, title: str, summary: str = "") -> Signal:
        return Signal(source="x", source_type="rss", title=title,
                      url=f"https://e.example/{abs(hash(title))}",
                      published_at=_utcnow(), summary=summary)

    def test_funding(self) -> None:
        b = scorer.compute_boosters(self._sig("Acme raises $50M Series B"), set())
        self.assertEqual(b.get("funding"), 0.05)

    def test_m_and_a(self) -> None:
        b = scorer.compute_boosters(self._sig("CVS acquires Foo Health"), set())
        self.assertEqual(b.get("m_and_a"), 0.05)

    def test_regulatory(self) -> None:
        b = scorer.compute_boosters(self._sig("FDA approved new GLP-1 device"), set())
        self.assertEqual(b.get("regulatory"), 0.05)

    def test_listicle_penalty(self) -> None:
        b = scorer.compute_boosters(self._sig("10 best healthcare AI startups"), set())
        self.assertEqual(b.get("listicle"), -0.10)

    def test_opinion_penalty(self) -> None:
        b = scorer.compute_boosters(self._sig("Opinion: Why Medicare needs reform"), set())
        self.assertEqual(b.get("opinion"), -0.05)

    def test_tier1_voice(self) -> None:
        b = scorer.compute_boosters(
            self._sig("Andy Slavitt on Medicare reform", "by Andy Slavitt"),
            tier1_voice_names={"Andy Slavitt", "Ashish Jha"},
        )
        self.assertEqual(b.get("tier1_voice"), 0.10)

    def test_no_match(self) -> None:
        b = scorer.compute_boosters(self._sig("Hospital opens new wing"), set())
        self.assertEqual(b, {})


class ClusterTest(unittest.TestCase):
    def test_groups_near_duplicates(self) -> None:
        # Two near-identical (cos > 0.99), two distinct
        embeddings = [
            [1.0, 0.0],   # A
            [0.9, 0.1],   # near-A
            [0.0, 1.0],   # B (cos vs A = 0)
        ]
        clusters = scorer.cluster_signals(embeddings, threshold=0.85)
        self.assertEqual(len(clusters), 2)
        # The first cluster has indices 0 and 1
        self.assertIn([0, 1], clusters)
        self.assertIn([2], clusters)

    def test_below_threshold_no_merge(self) -> None:
        # Cosine ≈ 0.6 < 0.85 → separate clusters
        embeddings = [
            [1.0, 0.0],
            [0.6, 0.8],   # cos ≈ 0.6
        ]
        clusters = scorer.cluster_signals(embeddings, threshold=0.85)
        self.assertEqual(len(clusters), 2)


class PickCanonicalTest(unittest.TestCase):
    def test_longest_summary_wins(self) -> None:
        a = Signal(source="A", source_type="rss", title="t",
                   url="https://e.example/a", published_at=_utcnow(),
                   summary="short")
        b = Signal(source="B", source_type="rss", title="t",
                   url="https://e.example/b", published_at=_utcnow(),
                   summary="this is a much longer summary " * 5)
        self.assertEqual(scorer.pick_canonical([a, b]).source, "B")

    def test_tiebreak_by_pub_date(self) -> None:
        s = "same length summary  "
        a = Signal(source="A", source_type="rss", title="t",
                   url="https://e.example/a",
                   published_at=_utcnow() - timedelta(hours=2), summary=s)
        b = Signal(source="B", source_type="rss", title="t",
                   url="https://e.example/b",
                   published_at=_utcnow() - timedelta(hours=1), summary=s)
        # Earlier published_at wins on tie
        self.assertEqual(scorer.pick_canonical([a, b]).source, "A")


class ScoreStoryTest(unittest.TestCase):
    def test_clamps_to_one(self) -> None:
        chunks = [ScoredChunk(file_path="x", subfolder="y", chunk_index=0,
                              text="t", distance=0.0)]
        b = scorer.score_story(chunks, {"tier1_voice": 0.10, "funding": 0.05})
        self.assertLessEqual(b.final, 1.0)
        self.assertEqual(b.content_similarity, 1.0)

    def test_no_chunks_zero_content(self) -> None:
        b = scorer.score_story([], {"funding": 0.05})
        self.assertEqual(b.content_similarity, 0.0)
        self.assertAlmostEqual(b.final, 0.05)

    def test_clamps_to_zero(self) -> None:
        chunks = [ScoredChunk(file_path="x", subfolder="y", chunk_index=0,
                              text="t", distance=2.0)]  # similarity → 0
        b = scorer.score_story(chunks, {"listicle": -0.10})
        self.assertEqual(b.final, 0.0)


# --- End-to-end test ----------------------------------------------------

class RunScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = storage.connect(self.db_path)
        storage.init_db(conn=self.conn)
        self.conn.commit()

        self._patch_logs = mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name))
        self._patch_logs.start()
        self.chroma = chromadb.PersistentClient(path=str(Path(self.tmp.name) / "chroma"))

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_clusters_and_persists(self) -> None:
        now = _utcnow()
        # Two near-duplicates + one distinct + one already-sent
        s_dup1 = Signal(source="A", source_type="rss", title="Acme raises Series B",
                        url="https://e.example/a", published_at=now - timedelta(hours=2),
                        summary="A very thorough summary describing Acme's Series B funding round, the lead investor, and the use of proceeds.")
        s_dup2 = Signal(source="B", source_type="perplexity", title="Acme raises $50M",
                        url="https://e.example/b", published_at=now - timedelta(hours=3),
                        summary="Short.")
        s_distinct = Signal(source="C", source_type="rss", title="Pharma trial succeeds",
                            url="https://e.example/c", published_at=now - timedelta(hours=1),
                            summary="Trial readouts from Phase 3 study.")
        s_recent_sent = Signal(source="D", source_type="rss", title="Already-sent story",
                               url="https://sent.example/x", published_at=now - timedelta(hours=4),
                               summary="x")
        all_sigs = [s_dup1, s_dup2, s_distinct, s_recent_sent]
        storage.save_signals(all_sigs, conn=self.conn)

        # Mark the s_recent_sent URL as sent in the last 7 days.
        from models import Story
        sent_story = Story(
            id=story_id("https://sent.example/x"),
            canonical_url="https://sent.example/x",
            canonical_title="Already-sent",
            canonical_summary="",
            published_at=now - timedelta(days=2),
            relevance_score=0.5,
        )
        storage.upsert_story(sent_story, conn=self.conn)
        did = storage.create_digest("2026-05-03", ["a@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, sent_story.id, rank=1, conn=self.conn)
        storage.mark_digest_sent(did, now - timedelta(days=2), conn=self.conn)
        self.conn.commit()

        # Build a controlled embedder: two near-duplicates share a vector,
        # the distinct signal has a different vector. The recently-sent
        # signal's text never reaches the embedder (filtered out).
        # Sort order is (-pub_at, source, title): distinct (1h), s_dup1 (2h), s_dup2 (3h)
        text_distinct = scorer._signal_text(s_distinct)
        text_dup1 = scorer._signal_text(s_dup1)
        text_dup2 = scorer._signal_text(s_dup2)
        embedder = ControlledEmbedder({
            text_distinct: [0.0, 1.0],
            text_dup1:     [1.0, 0.0],
            text_dup2:     [0.95, 0.05],   # cos vs dup1 ≈ 0.998 → cluster merges
        })

        stats = scorer.run_scoring(
            conn=self.conn,
            chroma_client=self.chroma,
            embedder=embedder,
            voice_names=set(),
        )
        self.conn.commit()

        self.assertEqual(stats.signals_in, 4)
        self.assertEqual(stats.signals_filtered_recent, 1)
        self.assertEqual(stats.stories_created, 2)  # dups merged → 1 + distinct → 1

        # The sent signal stays unscored
        unscored = storage.list_unscored_signals(conn=self.conn)
        unscored_urls = {s.url for s in unscored}
        self.assertEqual(unscored_urls, {"https://sent.example/x"})

        # Both dup signals point at the same story
        dup_story_query = self.conn.execute(
            "SELECT story_id FROM signals WHERE url IN (?, ?)",
            (s_dup1.url, s_dup2.url),
        ).fetchall()
        story_ids = {r["story_id"] for r in dup_story_query}
        self.assertEqual(len(story_ids), 1)
        self.assertIsNotNone(next(iter(story_ids)))

        # Canonical for the dup cluster: s_dup1 (longer summary)
        canonical_id = story_id(s_dup1.url)
        canonical_row = self.conn.execute(
            "SELECT canonical_url FROM stories WHERE id = ?", (canonical_id,)
        ).fetchone()
        self.assertEqual(canonical_row["canonical_url"], "https://e.example/a")

    def test_historical_dup_filtered(self) -> None:
        """A new signal whose embedding matches a story sent in a prior digest
        should be dropped before scoring (not turn into a Story)."""
        from models import Story
        now = _utcnow()

        # Pretend we sent a story two days ago with embedding [1.0, 0.0].
        old_vec = [1.0, 0.0]
        old_url = "https://historical.example/a"
        old_sid = story_id(old_url)
        storage.upsert_story(
            Story(id=old_sid, canonical_url=old_url, canonical_title="Acme Series B",
                  canonical_summary="", published_at=now - timedelta(days=2),
                  relevance_score=0.7),
            embedding=old_vec, conn=self.conn,
        )
        did = storage.create_digest("2026-05-03", ["a@x.com"], conn=self.conn)
        storage.add_story_to_digest(did, old_sid, rank=1, conn=self.conn)
        storage.mark_digest_sent(did, now - timedelta(days=2), conn=self.conn)
        self.conn.commit()

        # Today's signal: different URL (so URL filter doesn't catch it) but
        # near-identical embedding to the historical story.
        new_sig = Signal(
            source="OtherOutlet", source_type="rss",
            title="Acme closes Series B round",
            url="https://newoutlet.example/acme-b",
            published_at=now - timedelta(hours=1),
            summary="Different wording, same event.",
        )
        storage.save_signals([new_sig], conn=self.conn)
        self.conn.commit()

        embedder = ControlledEmbedder({
            scorer._signal_text(new_sig): [0.99, 0.02],  # cos vs old_vec ≈ 0.9998
        })

        stats = scorer.run_scoring(
            conn=self.conn,
            chroma_client=self.chroma,
            embedder=embedder,
            voice_names=set(),
            firm_names=set(),
        )
        self.conn.commit()

        self.assertEqual(stats.clusters_filtered_historical, 1)
        self.assertEqual(stats.stories_created, 0)

        # The new signal stays unscored (story_id still NULL).
        unscored = storage.list_unscored_signals(conn=self.conn)
        self.assertIn(new_sig.url, {s.url for s in unscored})


if __name__ == "__main__":
    unittest.main()
