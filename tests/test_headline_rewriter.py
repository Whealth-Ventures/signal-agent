"""Tests for src/headline_rewriter.py — excerpt extraction, the rewrite pass,
and fail-soft behaviour. No network: fetches and the LLM client are faked."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import headline_rewriter  # noqa: E402
from models import Story  # noqa: E402
from perplexity_client import ChatResponse, PerplexityCallFailed  # noqa: E402
from ranker import RankedStory, RankingResult  # noqa: E402

_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _mk_story(sid: str, url: str) -> Story:
    return Story(
        id=sid,
        canonical_url=url,
        canonical_title=f"title {sid}",
        canonical_summary="",
        published_at=_TS,
        relevance_score=0.5,
        signal_ids=(),
    )


def _mk_ranking(pairs: list[tuple[str, str, str]]) -> RankingResult:
    """pairs: (story_id, url, one_liner). All stories land in top_summary."""
    ranked = [
        RankedStory(story=_mk_story(sid, url), tier="S", one_liner=ol)
        for sid, url, ol in pairs
    ]
    return RankingResult(
        top_summary=ranked,
        by_priority={},
        other=[],
        candidates_count=len(ranked),
        used_fallback=False,
        cost_usd=0.0,
        elapsed_seconds=0.0,
        flat=tuple(ranked),
    )


def _resp(payload: dict) -> ChatResponse:
    return ChatResponse(
        text=json.dumps(payload), citations=(), model="test",
        prompt_tokens=1, completion_tokens=1, estimated_cost_usd=0.0, raw={},
    )


class FakeClient:
    def __init__(self, payload: dict | Exception):
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if isinstance(self.payload, Exception):
            raise self.payload
        return _resp(self.payload)


def _fake_fetch(mapping: dict[str, str]):
    async def fetch(urls):
        return {u: mapping.get(u, "") for u in urls}
    return fetch


class ExtractExcerptTests(unittest.TestCase):
    def test_prefers_paragraphs_over_nav_chrome(self):
        html = (
            "<nav>Home News Radiology Cardiac Care</nav>"
            "<ul><li><a href='/x'>Menu item</a></li></ul>"
            "<p>NCDs caused 43 million deaths in 2021.</p>"
            "<p>The operating system has not kept pace.</p>"
            "<footer>Subscribe now</footer>"
        )
        out = headline_rewriter.extract_excerpt(html)
        self.assertIn("43 million deaths", out)
        self.assertIn("operating system", out)
        self.assertNotIn("Radiology", out)
        self.assertNotIn("Subscribe", out)

    def test_falls_back_to_stripped_text_without_paragraphs(self):
        out = headline_rewriter.extract_excerpt("<div>plain &amp; simple</div>")
        self.assertEqual(out, "plain & simple")

    def test_empty_and_limit(self):
        self.assertEqual(headline_rewriter.extract_excerpt(""), "")
        long = "<p>" + "word " * 2000 + "</p>"
        self.assertLessEqual(
            len(headline_rewriter.extract_excerpt(long)),
            headline_rewriter.EXCERPT_CHARS,
        )


class RewriteHeadlinesTests(unittest.TestCase):
    def setUp(self):
        self.ranking = _mk_ranking([
            ("s1", "https://a.example/1", "old headline one"),
            ("s2", "https://b.example/2", "old headline two"),
        ])
        # Silence the jsonl audit log.
        patcher = mock.patch.object(headline_rewriter, "_log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_replaces_one_liners_everywhere(self):
        client = FakeClient({"headlines": {
            "s1": "Tulu Health CEO: rebuild ops for continuous care",
            "s2": "old headline two",
        }})
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts",
            new=_fake_fetch({"https://a.example/1": "body text " * 20}),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ):
            out = headline_rewriter.rewrite_headlines(self.ranking)
        self.assertEqual(
            out.flat[0].one_liner,
            "Tulu Health CEO: rebuild ops for continuous care",
        )
        self.assertEqual(out.top_summary[0].one_liner, out.flat[0].one_liner)
        self.assertEqual(out.flat[1].one_liner, "old headline two")
        # One LLM call, carrying the excerpt.
        self.assertEqual(len(client.calls), 1)
        self.assertIn("body text", client.calls[0]["prompt"])

    def test_no_excerpts_fetched_skips_llm_call(self):
        client = FakeClient({"headlines": {}})
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts", new=_fake_fetch({}),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ):
            out = headline_rewriter.rewrite_headlines(self.ranking)
        self.assertIs(out, self.ranking)
        self.assertEqual(client.calls, [])

    def test_llm_failure_returns_ranking_unchanged(self):
        client = FakeClient(PerplexityCallFailed("boom"))
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts",
            new=_fake_fetch({"https://a.example/1": "body"}),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ):
            out = headline_rewriter.rewrite_headlines(self.ranking)
        self.assertIs(out, self.ranking)

    def test_overlong_or_missing_headlines_keep_original(self):
        client = FakeClient({"headlines": {
            "s1": "x" * (headline_rewriter.MAX_HEADLINE_CHARS + 1),
            # s2 absent entirely
        }})
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts",
            new=_fake_fetch({"https://a.example/1": "body"}),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ):
            out = headline_rewriter.rewrite_headlines(self.ranking)
        self.assertEqual(out.flat[0].one_liner, "old headline one")
        self.assertEqual(out.flat[1].one_liner, "old headline two")

    def test_empty_ranking_is_a_noop(self):
        empty = _mk_ranking([])
        self.assertIs(headline_rewriter.rewrite_headlines(empty), empty)


if __name__ == "__main__":
    unittest.main()
