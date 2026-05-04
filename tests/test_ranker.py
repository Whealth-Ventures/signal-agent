"""Smoke tests for src/ranker.py — fake PerplexityClient, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import ranker  # noqa: E402
import storage  # noqa: E402
from models import Story, story_id  # noqa: E402
from perplexity_client import ChatResponse  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_story(slug: str, score: float = 0.5, summary: str = "") -> Story:
    url = f"https://e.example/{slug}"
    return Story(
        id=story_id(url),
        canonical_url=url,
        canonical_title=f"Story {slug}",
        canonical_summary=summary or f"Summary for {slug}.",
        published_at=_utcnow(),
        relevance_score=score,
    )


class FakePerplexityClient:
    def __init__(self, response_text: str, *, cost: float = 0.005,
                 model: str = "sonar-reasoning") -> None:
        self.response_text = response_text
        self.cost = cost
        self.model = model
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, model: str = "", recency: str | None = None,
                 query_id: str = "", system: str | None = None) -> ChatResponse:
        self.calls.append({
            "prompt": prompt, "model": model, "query_id": query_id,
            "system": system,
        })
        return ChatResponse(
            text=self.response_text, citations=(),
            model=model or self.model,
            prompt_tokens=100, completion_tokens=50,
            estimated_cost_usd=self.cost, raw={},
        )


# --- Prompt build ------------------------------------------------------

class BuildPromptTest(unittest.TestCase):
    def test_includes_ids_and_titles(self) -> None:
        a = _mk_story("a", score=0.8)
        b = _mk_story("b", score=0.6)
        prompt = ranker.build_prompt([a, b], top_n=5)
        self.assertIn(a.id, prompt)
        self.assertIn(b.id, prompt)
        self.assertIn("Story a", prompt)
        self.assertIn("Story b", prompt)
        self.assertIn("0.800", prompt)

    def test_truncates_long_summary(self) -> None:
        long = "x" * 1000
        s = _mk_story("a", summary=long)
        prompt = ranker.build_prompt([s], top_n=5)
        self.assertLess(prompt.count("x"), 250)


# --- JSON extraction ---------------------------------------------------

class ExtractJsonTest(unittest.TestCase):
    def test_clean_json(self) -> None:
        obj = ranker._extract_json('{"ranked": []}')
        self.assertEqual(obj, {"ranked": []})

    def test_fenced_json(self) -> None:
        obj = ranker._extract_json('```json\n{"ranked": [{"x": 1}]}\n```')
        self.assertEqual(obj, {"ranked": [{"x": 1}]})

    def test_with_preamble(self) -> None:
        obj = ranker._extract_json('Sure! Here you go:\n{"ranked": []}')
        self.assertEqual(obj, {"ranked": []})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json("nope, just text"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json(""))


# --- Parse ranked ------------------------------------------------------

class ParseRankedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stories = [_mk_story(s, score=0.5 + i * 0.05)
                        for i, s in enumerate(("a", "b", "c", "d"))]
        self.by_id = {s.id: s for s in self.stories}

    def test_well_formed_json(self) -> None:
        a, b, c = self.stories[0], self.stories[1], self.stories[2]
        text = json.dumps({"ranked": [
            {"story_id": b.id, "rank": 1, "reasoning": "Most important"},
            {"story_id": a.id, "rank": 2, "reasoning": "Solid"},
        ]})
        ranked, fallback = ranker.parse_ranked(text, self.by_id, top_n=2)
        self.assertFalse(fallback)
        self.assertEqual([r.story.id for r in ranked], [b.id, a.id])
        self.assertEqual(ranked[0].reasoning, "Most important")

    def test_unknown_id_dropped_then_filled_from_score(self) -> None:
        a = self.stories[0]
        text = json.dumps({"ranked": [
            {"story_id": "definitely_not_an_id", "rank": 1, "reasoning": "x"},
            {"story_id": a.id, "rank": 2, "reasoning": "fine"},
        ]})
        ranked, fallback = ranker.parse_ranked(text, self.by_id, top_n=3)
        # Unknown id dropped; remaining slots filled from score order
        ids = [r.story.id for r in ranked]
        self.assertEqual(len(ids), 3)
        self.assertIn(a.id, ids)
        self.assertTrue(fallback)

    def test_garbage_falls_back_to_score_order(self) -> None:
        ranked, fallback = ranker.parse_ranked(
            "not json", self.by_id, top_n=2,
        )
        self.assertTrue(fallback)
        # Top 2 by score: index 3 (score 0.65) then index 2 (score 0.60)
        self.assertEqual(
            [r.story.id for r in ranked],
            [self.stories[3].id, self.stories[2].id],
        )


# --- Orchestrator ------------------------------------------------------

class _OrchestratorBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = storage.connect(self.db_path)
        storage.init_db(conn=self.conn)
        self._patch_logs = mock.patch.object(
            config, "LOGS_DIR", Path(self.tmp.name),
        )
        self._patch_logs.start()
        self.conn.commit()

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.conn.close()
        self.tmp.cleanup()


class EmptyPoolTest(_OrchestratorBase):
    def test_empty_returns_empty(self) -> None:
        client = FakePerplexityClient('{"ranked":[]}')
        result = ranker.rank_stories(conn=self.conn, client=client)
        self.assertEqual(result.ranked, [])
        self.assertEqual(client.calls, [])  # no LLM call


class ShortCircuitTest(_OrchestratorBase):
    def test_pool_smaller_than_top_n_skips_llm(self) -> None:
        for s in (_mk_story("a", 0.3), _mk_story("b", 0.7), _mk_story("c", 0.5)):
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        client = FakePerplexityClient('{"ranked":[]}')
        result = ranker.rank_stories(top_n=5, conn=self.conn, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(result.ranked), 3)
        self.assertEqual(result.ranked[0].story.canonical_url, "https://e.example/b")
        self.assertEqual(result.cost_usd, 0.0)


class FullPathTest(_OrchestratorBase):
    def test_llm_response_used(self) -> None:
        # 8 stories → must call LLM
        stories = [_mk_story(c, 0.4 + i * 0.05) for i, c in enumerate("abcdefgh")]
        for s in stories:
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()

        # Pick stories b, d, f, h, a in that order (mix of low + high scores)
        chosen = [stories[1], stories[3], stories[5], stories[7], stories[0]]
        text = json.dumps({"ranked": [
            {"story_id": s.id, "rank": i + 1, "reasoning": f"reason for {s.canonical_url[-1]}"}
            for i, s in enumerate(chosen)
        ]})
        client = FakePerplexityClient(text)

        result = ranker.rank_stories(top_n=5, conn=self.conn, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["model"], config.PERPLEXITY_MODEL_RANK)
        self.assertEqual(client.calls[0]["query_id"], "rank")
        self.assertIsNotNone(client.calls[0]["system"])
        self.assertEqual(len(result.ranked), 5)
        self.assertEqual(
            [r.story.id for r in result.ranked],
            [s.id for s in chosen],
        )
        self.assertFalse(result.used_fallback)

    def test_garbage_response_uses_fallback(self) -> None:
        stories = [_mk_story(c, 0.4 + i * 0.05) for i, c in enumerate("abcdefgh")]
        for s in stories:
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        client = FakePerplexityClient("LLM is sad today, no JSON for you")
        result = ranker.rank_stories(top_n=5, conn=self.conn, client=client)
        self.assertTrue(result.used_fallback)
        # Top 5 by score = h g f e d
        self.assertEqual(
            [r.story.id for r in result.ranked],
            [stories[7].id, stories[6].id, stories[5].id,
             stories[4].id, stories[3].id],
        )


class LoggingTest(_OrchestratorBase):
    def test_log_record_written(self) -> None:
        for s in (_mk_story("a", 0.7), _mk_story("b", 0.6)):
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        client = FakePerplexityClient('{"ranked":[]}')
        ranker.rank_stories(top_n=5, conn=self.conn, client=client)
        log_files = list(config.LOGS_DIR.glob("ranker_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        rec = json.loads(log_files[0].read_text().strip())
        self.assertEqual(rec["candidates_count"], 2)
        self.assertEqual(rec["top_n"], 5)


if __name__ == "__main__":
    unittest.main()
