"""Smoke tests for src/ranker.py — magnitude rubric, per-category selection,
top-5 summary, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def _mk_story(
    slug: str,
    *,
    score: float = 0.5,
    summary: str = "",
    priority_bucket: str | None = None,
    geo: str | None = None,
) -> Story:
    url = f"https://e.example/{slug}"
    return Story(
        id=story_id(url),
        canonical_url=url,
        canonical_title=f"Story {slug}",
        canonical_summary=summary or f"Summary for {slug}.",
        published_at=_utcnow(),
        relevance_score=score,
        priority_bucket=priority_bucket,
        geo=geo,
    )


class FakePerplexityClient:
    def __init__(self, response_text: str, *, cost: float = 0.005,
                 model: str = "sonar-reasoning") -> None:
        self.response_text = response_text
        self.cost = cost
        self.model = model
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, model: str = "", recency: str | None = None,
                 query_id: str = "", system: str | None = None,
                 timeout: float | None = None) -> ChatResponse:
        self.calls.append({
            "prompt": prompt, "model": model, "query_id": query_id,
            "system": system, "timeout": timeout,
        })
        return ChatResponse(
            text=self.response_text, citations=(),
            model=model or self.model,
            prompt_tokens=100, completion_tokens=50,
            estimated_cost_usd=self.cost, raw={},
        )


# --- Prompt build ------------------------------------------------------

class BuildPromptTest(unittest.TestCase):
    def test_groups_by_priority_bucket(self) -> None:
        stories = [
            _mk_story("a", score=0.8, priority_bucket="venture_ipo"),
            _mk_story("b", score=0.6, priority_bucket="fda_regulatory"),
            _mk_story("c", score=0.4, priority_bucket=None),
        ]
        grouped = ranker._group_for_prompt(stories)
        prompt = ranker.build_prompt(grouped)
        self.assertIn("Venture & IPO", prompt)
        self.assertIn("FDA & Regulatory", prompt)
        self.assertIn("Other", prompt)
        for s in stories:
            self.assertIn(s.id, prompt)

    def test_includes_magnitude_rubric(self) -> None:
        grouped = ranker._group_for_prompt([_mk_story("a")])
        prompt = ranker.build_prompt(grouped)
        self.assertIn("TIER S", prompt)
        self.assertIn("TIER C", prompt)

    def test_truncates_long_summary(self) -> None:
        long = "x" * 1000
        grouped = ranker._group_for_prompt([_mk_story("a", summary=long)])
        prompt = ranker.build_prompt(grouped)
        self.assertLess(prompt.count("x"), 300)


# --- JSON extraction ---------------------------------------------------

class ExtractJsonTest(unittest.TestCase):
    def test_clean_json(self) -> None:
        obj = ranker._extract_json('{"stories": []}')
        self.assertEqual(obj, {"stories": []})

    def test_fenced_json(self) -> None:
        obj = ranker._extract_json('```json\n{"stories": [{"x": 1}]}\n```')
        self.assertEqual(obj, {"stories": [{"x": 1}]})

    def test_with_preamble(self) -> None:
        obj = ranker._extract_json('Sure!\n{"stories": []}')
        self.assertEqual(obj, {"stories": []})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json("nope"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json(""))


# --- Tier coercion + parse ranked --------------------------------------

class CoerceTierTest(unittest.TestCase):
    def test_valid_tiers(self) -> None:
        for t in ("S", "A", "B", "C", "s", "a"):
            self.assertEqual(ranker._coerce_tier(t), t.upper())

    def test_invalid_tier(self) -> None:
        self.assertIsNone(ranker._coerce_tier("D"))
        self.assertIsNone(ranker._coerce_tier(None))
        self.assertIsNone(ranker._coerce_tier(""))


class ParseRankedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stories = [_mk_story(s, score=0.5 + i * 0.05)
                        for i, s in enumerate(("a", "b", "c"))]
        self.by_id = {s.id: s for s in self.stories}

    def test_well_formed_json(self) -> None:
        a, b, c = self.stories
        text = json.dumps({"stories": [
            {"story_id": b.id, "tier": "S", "one_liner": "Big news"},
            {"story_id": a.id, "tier": "A", "one_liner": "Decent news"},
            {"story_id": c.id, "tier": "C", "one_liner": "Drop me"},
        ]})
        decisions, fallback = ranker.parse_ranked(text, self.by_id)
        self.assertFalse(fallback)
        self.assertEqual(decisions[b.id], ("S", "Big news"))
        self.assertEqual(decisions[a.id], ("A", "Decent news"))
        self.assertEqual(decisions[c.id], ("C", "Drop me"))

    def test_garbage_marks_fallback(self) -> None:
        decisions, fallback = ranker.parse_ranked("not json", self.by_id)
        self.assertTrue(fallback)
        self.assertEqual(decisions, {})

    def test_unknown_id_dropped(self) -> None:
        text = json.dumps({"stories": [
            {"story_id": "definitely_not_an_id", "tier": "S", "one_liner": "x"},
        ]})
        decisions, _ = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(decisions, {})

    def test_invalid_tier_dropped(self) -> None:
        text = json.dumps({"stories": [
            {"story_id": self.stories[0].id, "tier": "D", "one_liner": "x"},
        ]})
        decisions, _ = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(decisions, {})


# --- Selection ---------------------------------------------------------

class SelectionTest(unittest.TestCase):
    def test_keep_all_s(self) -> None:
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.5),
            _mk_story("s2", priority_bucket="venture_ipo", score=0.6),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("S", "y"),
        }
        by_priority, other = ranker._select(grouped, decisions, max_total=40)
        self.assertEqual(len(by_priority["venture_ipo"]), 2)
        self.assertEqual(other, [])

    def test_drop_tier_c(self) -> None:
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo"),
            _mk_story("s2", priority_bucket="venture_ipo"),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("C", "drop"),
        }
        by_priority, _ = ranker._select(grouped, decisions, max_total=40)
        self.assertEqual(len(by_priority["venture_ipo"]), 1)

    def test_b_kept_only_when_category_otherwise_empty(self) -> None:
        # First bucket has S+B → only S survives.
        # Second bucket has only B → that B survives (category would be empty).
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo"),
            _mk_story("s2", priority_bucket="venture_ipo"),
            _mk_story("s3", priority_bucket="ai_healthcare"),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("B", "y"),
            stories[2].id: ("B", "z"),
        }
        by_priority, _ = ranker._select(grouped, decisions, max_total=40)
        self.assertEqual(len(by_priority["venture_ipo"]), 1)
        self.assertEqual(by_priority["venture_ipo"][0].story.id, stories[0].id)
        self.assertEqual(len(by_priority["ai_healthcare"]), 1)

    def test_empty_other_isnt_artificially_filled(self) -> None:
        stories = [_mk_story("s1", priority_bucket=None)]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("B", "x")}
        _, other = ranker._select(grouped, decisions, max_total=40)
        # Other is never elevated by the "category empty → keep one B" rule.
        self.assertEqual(other, [])

    def test_target_min_zero_is_pure_threshold(self) -> None:
        # One S and three B in the same category, no floor → only the S survives.
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.9),
            _mk_story("b1", priority_bucket="venture_ipo", score=0.8),
            _mk_story("b2", priority_bucket="venture_ipo", score=0.7),
            _mk_story("b3", priority_bucket="venture_ipo", score=0.6),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("B", "y"),
            stories[2].id: ("B", "z"),
            stories[3].id: ("B", "w"),
        }
        by_priority, _ = ranker._select(grouped, decisions, max_total=40, target_min=0)
        self.assertEqual(len(by_priority["venture_ipo"]), 1)

    def test_backfill_to_floor_with_tier_b(self) -> None:
        # One S and three B; floor of 3 → backfill the two best B to reach 3.
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.9),
            _mk_story("b1", priority_bucket="venture_ipo", score=0.85),
            _mk_story("b2", priority_bucket="venture_ipo", score=0.80),
            _mk_story("b3", priority_bucket="venture_ipo", score=0.40),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("B", "y"),
            stories[2].id: ("B", "z"),
            stories[3].id: ("B", "w"),
        }
        by_priority, _ = ranker._select(grouped, decisions, max_total=40, target_min=3)
        chosen = by_priority["venture_ipo"]
        self.assertEqual(len(chosen), 3)
        ids = {r.story.id for r in chosen}
        # Highest-scoring B backfilled; the 0.40 one left out.
        self.assertIn(stories[1].id, ids)
        self.assertIn(stories[2].id, ids)
        self.assertNotIn(stories[3].id, ids)

    def test_backfill_never_resurrects_tier_c(self) -> None:
        # Floor is high but the only leftovers are Tier-C → they stay dropped.
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.9),
            _mk_story("c1", priority_bucket="venture_ipo", score=0.8),
            _mk_story("c2", priority_bucket="venture_ipo", score=0.7),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("C", "drop"),
            stories[2].id: ("C", "drop"),
        }
        by_priority, _ = ranker._select(grouped, decisions, max_total=40, target_min=10)
        self.assertEqual(len(by_priority["venture_ipo"]), 1)

    def test_backfill_respects_max_total(self) -> None:
        # Floor above the ceiling → ceiling wins.
        stories = [
            _mk_story(f"s{i}", priority_bucket="venture_ipo", score=0.9 - i * 0.01)
            for i in range(6)
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("S", "x")}
        decisions.update({s.id: ("B", "b") for s in stories[1:]})
        by_priority, _ = ranker._select(grouped, decisions, max_total=3, target_min=10)
        self.assertEqual(len(by_priority["venture_ipo"]), 3)

    def test_backfill_spans_categories_including_other(self) -> None:
        # Floor pulls the best leftover B regardless of category, incl. Other.
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.9),
            _mk_story("b_other", priority_bucket=None, score=0.88),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {
            stories[0].id: ("S", "x"),
            stories[1].id: ("B", "y"),
        }
        by_priority, other = ranker._select(
            grouped, decisions, max_total=40, target_min=2,
        )
        self.assertEqual(len(by_priority["venture_ipo"]), 1)
        self.assertEqual(len(other), 1)  # the Other B was backfilled to hit the floor


class TopSummaryTest(unittest.TestCase):
    def test_picks_n_highest_magnitude(self) -> None:
        rs = lambda slug, tier, score: ranker.RankedStory(
            story=_mk_story(slug, score=score, priority_bucket="venture_ipo"),
            tier=tier, one_liner="x",
        )
        by_priority = {"venture_ipo": [
            rs("a", "S", 0.5),
            rs("b", "A", 0.9),
            rs("c", "B", 0.95),
        ]}
        top = ranker._top_summary(by_priority, [], n=2)
        # S beats A and B regardless of score
        self.assertEqual([r.tier for r in top], ["S", "A"])

    def test_excludes_other(self) -> None:
        rs = lambda slug, tier, pri: ranker.RankedStory(
            story=_mk_story(slug, score=0.5, priority_bucket=pri),
            tier=tier, one_liner="x",
        )
        by_priority = {"venture_ipo": [rs("a", "A", "venture_ipo")]}
        other = [rs("b", "S", None)]
        top = ranker._top_summary(by_priority, other, n=5)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].story.id, _mk_story("a").id)


class RemovePromotedTest(unittest.TestCase):
    def test_drops_promoted_and_removes_empty_categories(self) -> None:
        rs = lambda slug: ranker.RankedStory(
            story=_mk_story(slug, score=0.5), tier="S", one_liner="x",
        )
        a, b = rs("a"), rs("b")
        by_priority = {"venture_ipo": [a], "fda_regulatory": [b]}
        # Both promoted → both categories empty → both hidden
        after = ranker._remove_promoted(by_priority, [a, b])
        self.assertEqual(after, {})


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
        client = FakePerplexityClient('{"stories":[]}')
        result = ranker.rank_stories(conn=self.conn, client=client)
        self.assertEqual(result.top_summary, [])
        self.assertEqual(result.by_priority, {})
        self.assertEqual(result.other, [])
        self.assertEqual(client.calls, [])  # no LLM call


class FullPathTest(_OrchestratorBase):
    def _seed(self) -> list[Story]:
        stories = [
            _mk_story("a", score=0.85, priority_bucket="fda_regulatory", geo="US"),
            _mk_story("b", score=0.78, priority_bucket="pe_strategics", geo="US"),
            _mk_story("c", score=0.74, priority_bucket="hospital_ma", geo="India"),
            _mk_story("d", score=0.71, priority_bucket="venture_ipo", geo="US"),
            _mk_story("e", score=0.68, priority_bucket="venture_ipo", geo="India"),
            _mk_story("f", score=0.55, priority_bucket="ai_healthcare", geo="Global"),
            _mk_story("g", score=0.30, priority_bucket=None, geo=None),
        ]
        for s in stories:
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        return stories

    def test_llm_response_used(self) -> None:
        stories = self._seed()
        decisions = {
            stories[0].id: ("S", "FDA approves something"),
            stories[1].id: ("S", "KKR acquires"),
            stories[2].id: ("S", "Apollo deal"),
            stories[3].id: ("S", "Hims raises"),
            stories[4].id: ("A", "Sarvodaya files DRHP"),
            stories[5].id: ("B", "AI seed"),
            stories[6].id: ("C", "Drop me"),
        }
        text = json.dumps({"stories": [
            {"story_id": sid, "tier": tier, "one_liner": ol}
            for sid, (tier, ol) in decisions.items()
        ]})
        client = FakePerplexityClient(text)
        result = ranker.rank_stories(conn=self.conn, client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["model"], config.PERPLEXITY_MODEL_RANK)
        self.assertEqual(len(result.top_summary), 5)
        # All 4 S items should be in top 5; the 5th is the highest A.
        top_tiers = [r.tier for r in result.top_summary]
        self.assertEqual(top_tiers.count("S"), 4)
        self.assertEqual(top_tiers.count("A"), 1)
        # The Tier-C story is dropped.
        all_ids = {r.story.id for r in result.flat}
        self.assertNotIn(stories[6].id, all_ids)

    def test_garbage_response_falls_back_to_score_order(self) -> None:
        self._seed()
        client = FakePerplexityClient("no JSON here")
        result = ranker.rank_stories(conn=self.conn, client=client)
        self.assertTrue(result.used_fallback)
        # Fallback treats everything as Tier A and applies normal selection.
        self.assertGreater(len(result.flat), 0)


class ExcludeRecentlySentTest(_OrchestratorBase):
    def test_sent_stories_excluded_from_candidates(self) -> None:
        """High-scoring stories already shipped in a recent digest must not
        reach the ranker — otherwise evergreens win the candidate pool forever."""
        now = _utcnow()
        old_hi = _mk_story("old_hi", score=0.95, priority_bucket="venture_ipo")
        new_hi = _mk_story("new_hi", score=0.60, priority_bucket="venture_ipo")
        for s in (old_hi, new_hi):
            storage.upsert_story(s, conn=self.conn)
        did = storage.create_digest("2026-05-20", ["x"], conn=self.conn)
        storage.add_story_to_digest(did, old_hi.id, rank=1, conn=self.conn)
        storage.mark_digest_sent(did, now - timedelta(days=5), conn=self.conn)
        self.conn.commit()

        client = FakePerplexityClient('{"stories":[]}')
        result = ranker.rank_stories(conn=self.conn, client=client)

        self.assertEqual(result.candidates_count, 1)
        all_ids = {r.story.id for r in result.flat}
        self.assertNotIn(old_hi.id, all_ids)


class LoggingTest(_OrchestratorBase):
    def test_log_record_written(self) -> None:
        for s in (_mk_story("a", score=0.7, priority_bucket="venture_ipo"),
                  _mk_story("b", score=0.6, priority_bucket="fda_regulatory")):
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        client = FakePerplexityClient('{"stories":[]}')
        ranker.rank_stories(conn=self.conn, client=client)
        log_files = list(config.LOGS_DIR.glob("ranker_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        # File has 2+ records — read the LAST line (the summary)
        lines = log_files[0].read_text().strip().splitlines()
        rec = json.loads(lines[-1])
        self.assertEqual(rec["candidates_count"], 2)


if __name__ == "__main__":
    unittest.main()
