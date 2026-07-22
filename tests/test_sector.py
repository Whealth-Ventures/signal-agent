"""Smoke tests for src/sector.py — the weekly Sector Agent. No network.

Covers: portfolio loading, per-company plan building (the company tag that must
survive dedup), the material-impact ranker (LLM-success + fallback paths), and
the company-grouped Slack layout. Also asserts the daily fetch path keeps its
default recency (the one shared-code change was backward-compatible).
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import sector  # noqa: E402
from models import Story  # noqa: E402
from perplexity_client import ChatResponse  # noqa: E402


def _story(sid: str, company: str | None, *, title: str = "Regulatory shift") -> Story:
    url = f"https://e.example/{sid}"
    return Story(
        id=sid,
        canonical_url=url,
        canonical_title=f"{title} {sid}",
        canonical_summary="summary",
        published_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        relevance_score=0.5,
        bucket=company,
    )


def _resp(payload: dict) -> ChatResponse:
    return ChatResponse(
        text=json.dumps(payload), citations=(), model="fake",
        prompt_tokens=1, completion_tokens=1, estimated_cost_usd=0.02, raw={},
    )


class _FakeClient:
    def __init__(self, payload: dict | None = None, *, boom: bool = False):
        self.payload = payload
        self.boom = boom
        self.calls = 0

    def complete(self, prompt, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("ranker unavailable")
        return _resp(self.payload or {"items": []})


class PortfolioTest(unittest.TestCase):
    def test_load(self) -> None:
        cos = sector.load_portfolio()
        self.assertGreaterEqual(len(cos), 10)
        names = {c.name for c in cos}
        self.assertIn("BeatO", names)
        # Geo coerces cleanly for planning.
        self.assertTrue(all(c.name and c.sector for c in cos))

    def test_plans_one_per_company(self) -> None:
        cos = sector.load_portfolio()
        plans = sector.build_sector_plans(cos)
        self.assertEqual(len(plans), len(cos))
        # The load-bearing invariant: bucket == company name (rides through dedup).
        self.assertEqual([p.bucket for p in plans], [c.name for c in cos])
        self.assertTrue(all(p.track == "sector" for p in plans))
        self.assertTrue({p.geography for p in plans} <= {"India", "US", "Global"})
        # Company name appears in its own prompt.
        self.assertIn(cos[0].name, plans[0].prompt_text)


class RankImpactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cos = sector.load_portfolio()
        self.c0 = self.cos[0].name
        self.c1 = self.cos[1].name

    def test_success_drops_low_and_groups(self) -> None:
        stories = [_story("s1", self.c0), _story("s2", self.c1), _story("s3", "Not A Portfolio Co")]
        payload = {"items": [
            {"story_id": "s1", "company": self.c0, "materiality": "high",
             "direction": "negative", "one_liner": "GLP-1 price cut squeezes market"},
            {"story_id": "s2", "company": self.c1, "materiality": "low",
             "direction": "positive", "one_liner": "should be dropped"},
        ]}
        res = sector.rank_impact(stories, self.cos, client=_FakeClient(payload))
        self.assertFalse(res.used_fallback)
        self.assertEqual(len(res.flat), 1)
        self.assertEqual(list(res.grouped), [self.c0])
        self.assertEqual(res.grouped[self.c0][0].direction, "negative")

    def test_unknown_company_skipped(self) -> None:
        stories = [_story("s1", self.c0)]
        payload = {"items": [
            {"story_id": "s1", "company": "Totally Made Up", "materiality": "high",
             "direction": "positive", "one_liner": "x"},
        ]}
        res = sector.rank_impact(stories, self.cos, client=_FakeClient(payload))
        # Nothing valid parsed → fallback keeps the story by its surfacing company.
        self.assertTrue(res.used_fallback)
        self.assertEqual(list(res.grouped), [self.c0])

    def test_fallback_on_call_error(self) -> None:
        stories = [_story("s1", self.c0), _story("s2", "Unknown Co")]
        res = sector.rank_impact(stories, self.cos, client=_FakeClient(boom=True))
        self.assertTrue(res.used_fallback)
        # Only the story whose surfacing company is in the portfolio survives.
        self.assertEqual(len(res.flat), 1)
        self.assertEqual(res.flat[0].company, self.c0)

    def test_empty_pool(self) -> None:
        res = sector.rank_impact([], self.cos, client=_FakeClient({"items": []}))
        self.assertEqual(res.flat, ())
        self.assertEqual(res.grouped, {})


class BlocksTest(unittest.TestCase):
    def test_grouped_blocks_within_limits(self) -> None:
        cos = sector.load_portfolio()
        payload = {"items": [
            {"story_id": "s1", "company": cos[0].name, "materiality": "high",
             "direction": "negative", "one_liner": "competitor raised $50M"},
        ]}
        res = sector.rank_impact([_story("s1", cos[0].name)], cos, client=_FakeClient(payload))
        blocks = sector.build_sector_blocks(res, digest_date="2026-07-27")
        self.assertLessEqual(len(blocks), sector.slack_client.MAX_BLOCKS)
        self.assertIn("Sector Signal", blocks[0]["text"]["text"])
        # The company header shows up somewhere in the body.
        body = json.dumps(blocks)
        self.assertIn(cos[0].name, body)

    def test_empty_digest_block(self) -> None:
        res = sector.SectorResult(grouped={}, candidates_count=0, used_fallback=False,
                                  cost_usd=0.0, elapsed_seconds=0.0)
        blocks = sector.build_sector_blocks(res, digest_date="2026-07-27")
        self.assertEqual(len(blocks), 2)
        self.assertIn("No material sector developments", json.dumps(blocks))


class RecencyRegressionTest(unittest.TestCase):
    """The shared change (optional recency param) must not alter daily defaults."""

    def test_search_recent_defaults_to_config(self) -> None:
        from query_planner import QueryPlan
        captured: dict = {}

        class C:
            def complete(self, prompt, *, model, recency, query_id):
                captured["recency"] = recency
                return _resp({"stories": []})

        # Bind the real method to our stub so the default-arg logic runs.
        import perplexity_client as pc
        plan = QueryPlan(id="p", geography="India", bucket="b", sub_buckets=(),
                         keyword_sample=(), keyword_count_total=0, prompt_text="x",
                         track="A")
        pc.PerplexityClient.search_recent(C(), plan)
        self.assertEqual(captured["recency"], config.PERPLEXITY_RECENCY)
        captured.clear()
        pc.PerplexityClient.search_recent(C(), plan, recency="week")
        self.assertEqual(captured["recency"], "week")


if __name__ == "__main__":
    unittest.main()
