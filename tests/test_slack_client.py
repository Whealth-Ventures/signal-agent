"""Smoke tests for src/slack_client.py — httpx.MockTransport, no network."""
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
from ranker import RankedStory  # noqa: E402


WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/xxx"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ranked(
    slug: str,
    rank: int = 1,
    one_liner: str = "",
    domain: str = "Oncology",
) -> RankedStory:
    url = f"https://e.example/{slug}"
    return RankedStory(
        story=Story(
            id=story_id(url),
            canonical_url=url,
            canonical_title=f"Title for {slug}",
            canonical_summary=f"Summary for {slug}.",
            published_at=_utcnow(),
            relevance_score=0.7,
        ),
        rank=rank,
        one_liner=one_liner or f"One-liner for {slug}",
        domain=domain,
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

    def test_403_falls_back_to_get(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403 if req.method == "HEAD" else 200)
        http = _http_with_routes(handler)
        self.assertTrue(slack_client.validate_url("https://e.example/x", http=http))

    def test_connect_error_invalid(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")
        http = _http_with_routes(handler)
        self.assertFalse(slack_client.validate_url("https://e.example/dead", http=http))


class BuildBlocksTest(unittest.TestCase):
    def test_blocks_contain_urls_one_liners_and_domain_headers(self) -> None:
        items = [
            _mk_ranked("a", rank=1, one_liner="Why a matters", domain="Oncology"),
            _mk_ranked("b", rank=2, one_liner="Why b matters", domain="Cardiology"),
        ]
        blocks = slack_client.build_blocks(items, digest_date="2026-05-05")
        flat = json.dumps(blocks)
        for r in items:
            self.assertIn(r.story.canonical_url, flat)
            self.assertIn(r.one_liner, flat)
        self.assertIn("ONCOLOGY", flat)
        self.assertIn("CARDIOLOGY", flat)
        self.assertIn("2026-05-05", flat)
        # First block must be a header
        self.assertEqual(blocks[0]["type"], "header")

    def test_groups_same_domain_under_one_section(self) -> None:
        items = [
            _mk_ranked("a", rank=1, domain="Oncology"),
            _mk_ranked("b", rank=2, domain="Oncology"),
            _mk_ranked("c", rank=3, domain="Cardiology"),
        ]
        blocks = slack_client.build_blocks(items, digest_date="2026-05-05")
        section_texts = [
            b["text"]["text"] for b in blocks
            if b["type"] == "section"
        ]
        # One section per domain
        self.assertEqual(len(section_texts), 2)
        # Both Oncology stories live inside the same section
        oncology_section = next(t for t in section_texts if "ONCOLOGY" in t)
        self.assertIn("https://e.example/a", oncology_section)
        self.assertIn("https://e.example/b", oncology_section)

    def test_empty_renders_no_stories_message(self) -> None:
        blocks = slack_client.build_blocks([], digest_date="2026-05-05")
        flat = json.dumps(blocks)
        self.assertIn("No stories", flat)

    def test_one_liner_escapes_mrkdwn_specials(self) -> None:
        item = RankedStory(
            story=Story(
                id="x", canonical_url="https://e.example/x",
                canonical_title="benign title",
                canonical_summary="x", published_at=_utcnow(),
                relevance_score=0.5,
            ),
            rank=1,
            one_liner="<script>alert(1)</script> & friends",
            domain="Oncology",
        )
        blocks = slack_client.build_blocks([item], digest_date="2026-05-05")
        flat = json.dumps(blocks)
        # Raw angle brackets would break Slack's <url|text> link syntax.
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


class PostHappyPathTest(_PostTestBase):
    def test_posts_with_correct_payload(self) -> None:
        seen: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and "hooks.slack.com" in str(req.url):
                seen["url"] = str(req.url)
                seen["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            # URL validation HEAD requests
            return httpx.Response(200)
        http = _http_with_routes(handler)
        items = [_mk_ranked("a", rank=1, one_liner="a"),
                 _mk_ranked("b", rank=2, one_liner="b")]
        result = slack_client.post_digest(
            items, digest_date="2026-05-05", http=http,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 2)
        self.assertEqual(result.stories_dropped_invalid_url, 0)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(seen["url"], WEBHOOK_URL)
        self.assertIn("blocks", seen["body"])
        self.assertIn("text", seen["body"])
        # Blocks payload should include both story URLs
        flat = json.dumps(seen["body"]["blocks"])
        self.assertIn("https://e.example/a", flat)
        self.assertIn("https://e.example/b", flat)


class PostDropsInvalidUrlTest(_PostTestBase):
    def test_invalid_url_dropped(self) -> None:
        good = _mk_ranked("good", rank=1, one_liner="ok")
        bad = _mk_ranked("bad", rank=2, one_liner="should drop")
        posted: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and "hooks.slack.com" in str(req.url):
                posted["body"] = json.loads(req.content)
                return httpx.Response(200, text="ok")
            # HEAD/GET URL validation: 404 only for /bad
            if "bad" in str(req.url):
                return httpx.Response(404)
            return httpx.Response(200)

        http = _http_with_routes(handler)
        result = slack_client.post_digest(
            [good, bad], digest_date="2026-05-05", http=http,
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
            [_mk_ranked("a")], digest_date="2026-05-05", http=http,
            skip_url_validation=True,
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
            [_mk_ranked("a")], digest_date="2026-05-05", http=http,
            skip_url_validation=True,
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
            [_mk_ranked("a")], digest_date="2026-05-05", http=http,
            skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertIn("ConnectError", result.error or "")

    def test_no_webhook_url_short_circuits(self) -> None:
        with mock.patch.object(config, "SLACK_WEBHOOK_URL", ""):
            result = slack_client.post_digest(
                [_mk_ranked("a")], digest_date="2026-05-05",
                skip_url_validation=True,
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
            [], digest_date="2026-05-05", http=http, skip_url_validation=True,
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
            [_mk_ranked("a")], digest_date="2026-05-05", http=http,
            skip_url_validation=True,
        )
        log_files = list(Path(self.tmp.name).glob("slack_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        rec = json.loads(log_files[0].read_text().strip())
        self.assertTrue(rec["sent"])
        self.assertEqual(rec["digest_date"], "2026-05-05")
        self.assertEqual(rec["stories_sent"], 1)
        self.assertEqual(rec["channel_label"], "#healthcare-signal")


if __name__ == "__main__":
    unittest.main()
