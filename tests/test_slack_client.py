"""Smoke tests for src/slack_client.py — new format, httpx.MockTransport, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import slack_client  # noqa: E402
from models import Story, story_id  # noqa: E402
from ranker import RankedStory, RankingResult  # noqa: E402


WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/xxx"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_story(
    slug: str,
    *,
    priority_bucket: str | None = None,
    geo: str | None = None,
) -> Story:
    url = f"https://e.example/{slug}"
    return Story(
        id=story_id(url),
        canonical_url=url,
        canonical_title=f"Title for {slug}",
        canonical_summary=f"Summary for {slug}.",
        published_at=_utcnow(),
        relevance_score=0.7,
        priority_bucket=priority_bucket,
        geo=geo,
    )


def _mk_ranked(
    slug: str,
    *,
    tier: str = "A",
    one_liner: str = "",
    priority_bucket: str | None = None,
    geo: str | None = None,
) -> RankedStory:
    return RankedStory(
        story=_mk_story(slug, priority_bucket=priority_bucket, geo=geo),
        tier=tier,
        one_liner=one_liner or f"One-liner for {slug}",
    )


def _mk_ranking(
    *,
    top_summary: list[RankedStory] | None = None,
    by_priority: dict[str, list[RankedStory]] | None = None,
    other: list[RankedStory] | None = None,
) -> RankingResult:
    top = top_summary or []
    bp = by_priority or {}
    oth = other or []
    flat = (
        list(top)
        + [r for b in config.PRIORITY_BUCKETS for r in bp.get(b.key, [])]
        + list(oth)
    )
    return RankingResult(
        top_summary=top, by_priority=bp, other=oth,
        candidates_count=len(flat),
        used_fallback=False, cost_usd=0.0, elapsed_seconds=0.1,
        flat=tuple(flat),
    )


def _http_with_routes(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class ValidateUrlTest(unittest.TestCase):
    def test_200_valid(self) -> None:
        http = _http_with_routes(lambda req: httpx.Response(200))
        self.assertTrue(slack_client.validate_url("https://e.example/ok", http=http))

    def test_404_invalid(self) -> None:
        http = _http_with_routes(lambda req: httpx.Response(404))
        self.assertFalse(slack_client.validate_url("https://e.example/nope", http=http))

    def test_405_falls_back_to_get(self) -> None:
        seen = {"head": 0, "get": 0}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "HEAD":
                seen["head"] += 1
                return httpx.Response(405)
            seen["get"] += 1
            return httpx.Response(200)
        http = _http_with_routes(handler)
        self.assertTrue(slack_client.validate_url("https://e.example/x", http=http))
        self.assertEqual(seen["head"], 1)
        self.assertEqual(seen["get"], 1)

    def test_connect_error_invalid(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")
        http = _http_with_routes(handler)
        self.assertFalse(slack_client.validate_url("https://e.example/dead", http=http))


class GeoTagTest(unittest.TestCase):
    def test_india_tag(self) -> None:
        self.assertEqual(slack_client._geo_tag("India"), "[IND] ")

    def test_us_tag(self) -> None:
        self.assertEqual(slack_client._geo_tag("US"), "[US]  ")

    def test_global_has_no_tag(self) -> None:
        self.assertEqual(slack_client._geo_tag("Global"), "")

    def test_none_has_no_tag(self) -> None:
        self.assertEqual(slack_client._geo_tag(None), "")


class BulletTest(unittest.TestCase):
    def test_format_with_india_tag(self) -> None:
        r = _mk_ranked("a", one_liner="something happened", geo="India")
        b = slack_client._bullet(r)
        self.assertTrue(b.startswith("• [IND] something happened"))
        self.assertIn("(<https://e.example/a|Link>)", b)
        # Source name is NEVER in the bullet, only the (Link) hyperlink.
        self.assertNotIn("e.example", b.replace("https://e.example/a", ""))

    def test_format_global_has_no_tag(self) -> None:
        r = _mk_ranked("a", one_liner="x", geo="Global")
        b = slack_client._bullet(r)
        self.assertNotIn("[IND]", b)
        self.assertNotIn("[US]", b)
        self.assertNotIn("[GLB]", b)
        self.assertNotIn("[Global]", b)


class BuildBlocksTest(unittest.TestCase):
    def test_header_has_date_and_total_count(self) -> None:
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("a", geo="US")],
            by_priority={"venture_ipo": [_mk_ranked("b", geo="India")]},
        )
        blocks = slack_client.build_blocks(ranking, digest_date="Wed, 27 May 2026")
        flat = json.dumps(blocks)
        self.assertIn("Wed, 27 May 2026", flat)
        self.assertIn("2 stories", flat)

    def test_top_summary_section_present(self) -> None:
        ranking = _mk_ranking(
            top_summary=[
                _mk_ranked("a", one_liner="FDA approves something", geo="US"),
                _mk_ranked("b", one_liner="KKR acquires", geo="US"),
            ],
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        flat = json.dumps(blocks)
        self.assertIn("Today's biggest stories", flat)
        self.assertIn("FDA approves something", flat)
        self.assertIn("KKR acquires", flat)

    def test_priority_sections_titled_by_display_name_with_counts(self) -> None:
        ranking = _mk_ranking(
            by_priority={
                "venture_ipo": [_mk_ranked("a"), _mk_ranked("b")],
                "fda_regulatory": [_mk_ranked("c")],
            },
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        flat = json.dumps(blocks)
        self.assertIn("Venture &amp; IPO* (2)", flat)
        self.assertIn("FDA &amp; Regulatory* (1)", flat)

    def test_priority_sections_in_config_order(self) -> None:
        ranking = _mk_ranking(
            by_priority={
                # Reverse insertion order — must still come out in config order.
                "us_medicare":   [_mk_ranked("c")],
                "venture_ipo":   [_mk_ranked("a")],
                "fda_regulatory":[_mk_ranked("b")],
            },
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        section_texts = [
            b["text"]["text"] for b in blocks if b["type"] == "section"
        ]
        # venture_ipo (#1 in config) → fda_regulatory (#5) → us_medicare (#7)
        venture_idx = next(i for i, t in enumerate(section_texts) if "Venture" in t)
        fda_idx = next(i for i, t in enumerate(section_texts) if "FDA" in t)
        medicare_idx = next(i for i, t in enumerate(section_texts) if "Medicare" in t)
        self.assertLess(venture_idx, fda_idx)
        self.assertLess(fda_idx, medicare_idx)

    def test_empty_priority_categories_are_hidden(self) -> None:
        ranking = _mk_ranking(
            by_priority={"venture_ipo": [_mk_ranked("a")]},
            # All other 7 priority buckets are absent → must not appear.
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        flat = json.dumps(blocks)
        for b in config.PRIORITY_BUCKETS:
            if b.key == "venture_ipo":
                continue
            self.assertNotIn(b.display, flat,
                             f"Empty category {b.display} should be hidden")

    def test_other_section_appears_at_bottom(self) -> None:
        ranking = _mk_ranking(
            by_priority={"venture_ipo": [_mk_ranked("a")]},
            other=[_mk_ranked("z", one_liner="long-tail story")],
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        section_texts = [
            b["text"]["text"] for b in blocks if b["type"] == "section"
        ]
        # "Other healthcare news" should be the last section
        self.assertIn("Other healthcare news", section_texts[-1])
        self.assertIn("long-tail story", section_texts[-1])

    def test_empty_digest_renders_no_stories_message(self) -> None:
        ranking = _mk_ranking()
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        flat = json.dumps(blocks)
        self.assertIn("0 stories", flat)
        self.assertIn("No stories", flat)

    def test_test_mode_prefixes_header(self) -> None:
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("a", geo="US")],
        )
        blocks = slack_client.build_blocks(
            ranking, digest_date="2026-05-27", test_mode=True,
        )
        # Header is the first section's text
        first_text = blocks[0]["text"]["text"]
        self.assertIn("[TEST]", first_text)
        self.assertIn("Daily Healthcare Signal", first_text)
        # Marker is in the bold portion (before the closing `*`)
        self.assertTrue(first_text.startswith("*[TEST] Daily"),
                        f"unexpected header: {first_text!r}")

    def test_default_has_no_test_marker(self) -> None:
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("a", geo="US")],
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        self.assertNotIn("[TEST]", json.dumps(blocks))

    def test_one_liner_escapes_mrkdwn_specials(self) -> None:
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("x", one_liner="<script>alert(1)</script> & co", geo="US")],
        )
        blocks = slack_client.build_blocks(ranking, digest_date="2026-05-27")
        flat = json.dumps(blocks)
        self.assertNotIn("<script>", flat)
        self.assertIn("&lt;script&gt;", flat)
        self.assertIn("&amp;", flat)


class _PostTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name)),
            mock.patch.object(config, "SLACK_WEBHOOK_URL", WEBHOOK_URL),
            mock.patch.object(config, "SLACK_CHANNEL_LABEL", "#healthcare-signal"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()


class PostTestModeTest(_PostTestBase):
    def test_test_mode_payload_carries_marker(self) -> None:
        seen: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and "hooks.slack.com" in str(req.url):
                seen["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("a", one_liner="a", geo="US")],
        )
        result = slack_client.post_digest(
            ranking, digest_date="2026-05-27", http=http, test_mode=True,
        )
        self.assertTrue(result.sent)
        # Fallback text (shown in notifications) carries the marker
        self.assertTrue(seen["body"]["text"].startswith("[TEST] "),
                        f"fallback text missing marker: {seen['body']['text']!r}")
        # And the in-channel header has it too
        header = seen["body"]["blocks"][0]["text"]["text"]
        self.assertIn("[TEST]", header)


class PostHappyPathTest(_PostTestBase):
    def test_posts_with_correct_payload(self) -> None:
        seen: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and "hooks.slack.com" in str(req.url):
                seen["url"] = str(req.url)
                seen["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("a", one_liner="a", geo="US")],
            by_priority={"venture_ipo": [_mk_ranked("b", one_liner="b", geo="India")]},
        )
        result = slack_client.post_digest(
            ranking, digest_date="2026-05-27", http=http,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 2)
        self.assertEqual(result.stories_dropped_invalid_url, 0)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(seen["url"], WEBHOOK_URL)
        flat = json.dumps(seen["body"]["blocks"])
        self.assertIn("https://e.example/a", flat)
        self.assertIn("https://e.example/b", flat)


class PostDropsInvalidUrlTest(_PostTestBase):
    def test_invalid_url_dropped(self) -> None:
        posted: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and "hooks.slack.com" in str(req.url):
                posted["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            if "bad" in str(req.url):
                return httpx.Response(404)
            return httpx.Response(200)
        http = _http_with_routes(handler)
        ranking = _mk_ranking(
            top_summary=[_mk_ranked("good", one_liner="ok", geo="US")],
            by_priority={"venture_ipo": [_mk_ranked("bad", one_liner="drop me", geo="US")]},
        )
        result = slack_client.post_digest(
            ranking, digest_date="2026-05-27", http=http,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 1)
        self.assertEqual(result.stories_dropped_invalid_url, 1)
        flat = json.dumps(posted["body"]["blocks"])
        self.assertNotIn("https://e.example/bad", flat)
        self.assertIn("https://e.example/good", flat)


class PostFailureTest(_PostTestBase):
    def test_non_200_returns_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return httpx.Response(403, text="invalid_token")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        result = slack_client.post_digest(
            _mk_ranking(top_summary=[_mk_ranked("a")]),
            digest_date="2026-05-27", http=http, skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertEqual(result.status_code, 403)
        self.assertIn("invalid_token", result.error or "")

    def test_200_with_non_ok_body_returns_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return httpx.Response(200, text="no_text")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        result = slack_client.post_digest(
            _mk_ranking(top_summary=[_mk_ranked("a")]),
            digest_date="2026-05-27", http=http, skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertIn("no_text", result.error or "")

    def test_http_error_returns_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                raise httpx.ConnectError("no route to host")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        result = slack_client.post_digest(
            _mk_ranking(top_summary=[_mk_ranked("a")]),
            digest_date="2026-05-27", http=http, skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertIn("ConnectError", result.error or "")

    def test_no_webhook_url_short_circuits(self) -> None:
        with mock.patch.object(config, "SLACK_WEBHOOK_URL", ""):
            result = slack_client.post_digest(
                _mk_ranking(top_summary=[_mk_ranked("a")]),
                digest_date="2026-05-27", skip_url_validation=True,
            )
        self.assertFalse(result.sent)
        self.assertEqual(result.error, "SLACK_WEBHOOK_URL not configured")


class PostEmptyDigestTest(_PostTestBase):
    def test_empty_digest_still_posts(self) -> None:
        posted: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                posted["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        result = slack_client.post_digest(
            _mk_ranking(), digest_date="2026-05-27", http=http,
            skip_url_validation=True,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 0)
        self.assertIn("No stories", json.dumps(posted["body"]["blocks"]))


class LoggingTest(_PostTestBase):
    def test_log_record_written(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return httpx.Response(200, text="ok")
            return httpx.Response(200)
        http = _http_with_routes(handler)
        slack_client.post_digest(
            _mk_ranking(top_summary=[_mk_ranked("a")]),
            digest_date="2026-05-27", http=http, skip_url_validation=True,
        )
        log_files = list(Path(self.tmp.name).glob("slack_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        rec = json.loads(log_files[0].read_text().strip())
        self.assertTrue(rec["sent"])
        self.assertEqual(rec["digest_date"], "2026-05-27")
        self.assertEqual(rec["stories_sent"], 1)
        self.assertEqual(rec["channel_label"], "#healthcare-signal")


if __name__ == "__main__":
    unittest.main()
