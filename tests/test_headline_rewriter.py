"""Tests for src/headline_rewriter.py — excerpt extraction, the rewrite pass,
and fail-soft behaviour. No network: fetches and the LLM client are faked."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

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


def _mk_ranking_bucketed(
    top: list[tuple[str, str, str]],
    buckets: dict[str, list[tuple[str, str, str]]],
) -> RankingResult:
    """A ranking shaped like a real digest: a top-summary AND bucket sections.

    Most of a live digest lives in `by_priority`, so the rewrite has to reach
    it, not just `top_summary`.
    """
    def mk(pairs):
        return [
            RankedStory(story=_mk_story(sid, url), tier="A", one_liner=ol)
            for sid, url, ol in pairs
        ]
    top_ranked = mk(top)
    by_priority = {k: mk(v) for k, v in buckets.items()}
    flat = list(top_ranked)
    for v in by_priority.values():
        flat.extend(v)
    return RankingResult(
        top_summary=top_ranked,
        by_priority=by_priority,
        other=[],
        candidates_count=len(flat),
        used_fallback=False,
        cost_usd=0.0,
        elapsed_seconds=0.0,
        flat=tuple(flat),
    )


def _resp(payload: dict) -> ChatResponse:
    return ChatResponse(
        text=json.dumps(payload), citations=(), model="test",
        prompt_tokens=1, completion_tokens=1, estimated_cost_usd=0.0, raw={},
    )


class FakeClient:
    def __init__(self, payload: dict | str | Exception):
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, str):
            # Raw, unparseable body — the degraded-vendor case.
            return ChatResponse(
                text=self.payload, citations=(), model="test",
                prompt_tokens=1, completion_tokens=1,
                estimated_cost_usd=0.0, raw={},
            )
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

    def test_overlong_headline_is_trimmed_at_a_word_boundary(self):
        cap = headline_rewriter.MAX_HEADLINE_CHARS
        # Words, so there IS a boundary to cut on; overshoot the cap.
        long = " ".join(["alpha"] * (cap // 5 + 4))
        client = FakeClient({"headlines": {
            "s1": long,
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
        got = out.flat[0].one_liner
        # Trimmed, not discarded: within the cap, no dangling partial word.
        self.assertLessEqual(len(got), cap)
        self.assertNotEqual(got, "old headline one")
        self.assertTrue(got.startswith("alpha alpha"))
        self.assertTrue(all(w == "alpha" for w in got.split()))
        # A story the LLM omitted keeps the ranker's one-liner.
        self.assertEqual(out.flat[1].one_liner, "old headline two")

    def test_unusable_headline_values_keep_original(self):
        client = FakeClient({"headlines": {
            "s1": "   ",   # whitespace only
            "s2": 42,       # not a string
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

    def test_rewrites_reach_bucket_sections_not_just_top_summary(self):
        ranking = _mk_ranking_bucketed(
            top=[("s1", "https://a.example/1", "old top")],
            buckets={
                "venture_ipo": [("s2", "https://b.example/2", "old vc")],
                "hospital_ma": [("s3", "https://c.example/3", "old ma")],
            },
        )
        client = FakeClient({"headlines": {
            "s1": "new top line",
            "s2": "new vc line",
            "s3": "new ma line",
        }})
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts",
            new=_fake_fetch({
                "https://a.example/1": "body a",
                "https://b.example/2": "body b",
                "https://c.example/3": "body c",
            }),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ):
            out = headline_rewriter.rewrite_headlines(ranking)
        self.assertEqual(out.top_summary[0].one_liner, "new top line")
        self.assertEqual(out.by_priority["venture_ipo"][0].one_liner, "new vc line")
        self.assertEqual(out.by_priority["hospital_ma"][0].one_liner, "new ma line")
        self.assertEqual(
            [r.one_liner for r in out.flat],
            ["new top line", "new vc line", "new ma line"],
        )

    def test_parse_failure_is_logged_with_the_response_body(self):
        """A parse failure must be distinguishable from 'kept every headline'."""
        client = FakeClient("not json at all")
        with mock.patch.object(
            headline_rewriter, "_fetch_excerpts",
            new=_fake_fetch({"https://a.example/1": "body"}),
        ), mock.patch.object(
            headline_rewriter, "_build_ranker_client",
            return_value=(client, "test-model"),
        ), mock.patch.object(headline_rewriter, "_log") as log:
            out = headline_rewriter.rewrite_headlines(self.ranking)
        self.assertEqual(out.flat[0].one_liner, "old headline one")
        events = [c.args[0] for c in log.call_args_list]
        failures = [e for e in events if e.get("event") == "parse_failed"]
        self.assertEqual(len(failures), 1)
        self.assertIn("not json at all", failures[0]["response_text"])


class FetchExcerptsTests(unittest.TestCase):
    """The real _fetch_excerpts, unmocked — this is the code with prod failure
    modes, and it was previously covered only through a fake."""

    def test_one_bad_url_does_not_kill_the_other_fetches(self):
        # httpx.InvalidURL is NOT an httpx.HTTPError, and a digest URL is only
        # as clean as the source that published it. Before the broadened
        # except + return_exceptions this aborted every fetch in the run.
        import asyncio as _asyncio

        good = "https://good.example/ok"
        bad = "http://"          # raises httpx.InvalidURL inside http.get

        class FakeResp:
            status_code = 200
            text = "<p>real article body</p>"

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                if url == bad:
                    raise httpx.InvalidURL("no host")
                return FakeResp()

        with mock.patch.object(httpx, "AsyncClient", FakeAsyncClient):
            out = _asyncio.run(headline_rewriter._fetch_excerpts([bad, good]))
        self.assertEqual(out[bad], "")
        self.assertIn("real article body", out[good])

    def test_non_http_exception_degrades_to_an_empty_excerpt(self):
        import asyncio as _asyncio

        url = "https://boom.example/1"

        class FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, _url):
                raise ValueError("something the transport never promised")

        with mock.patch.object(httpx, "AsyncClient", FakeAsyncClient):
            out = _asyncio.run(headline_rewriter._fetch_excerpts([url]))
        self.assertEqual(out[url], "")


if __name__ == "__main__":
    unittest.main()
