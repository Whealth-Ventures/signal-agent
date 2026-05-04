"""Smoke tests for src/emailer.py — mock SMTP + httpx.MockTransport, no network."""
from __future__ import annotations

import json
import smtplib
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
import emailer  # noqa: E402
from models import Story, story_id  # noqa: E402
from ranker import RankedStory  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ranked(slug: str, rank: int = 1, reasoning: str = "") -> RankedStory:
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
        reasoning=reasoning,
    )


def _http_with_routes(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class ValidateUrlTest(unittest.TestCase):
    def test_200_valid(self) -> None:
        http = _http_with_routes(lambda req: httpx.Response(200))
        self.assertTrue(emailer.validate_url("https://e.example/ok", http=http))

    def test_404_invalid(self) -> None:
        http = _http_with_routes(lambda req: httpx.Response(404))
        self.assertFalse(emailer.validate_url("https://e.example/nope", http=http))

    def test_405_falls_back_to_get(self) -> None:
        seen = {"head": 0, "get": 0}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "HEAD":
                seen["head"] += 1
                return httpx.Response(405)
            seen["get"] += 1
            return httpx.Response(200)
        http = _http_with_routes(handler)
        self.assertTrue(emailer.validate_url("https://e.example/x", http=http))
        self.assertEqual(seen["head"], 1)
        self.assertEqual(seen["get"], 1)

    def test_403_falls_back_to_get(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403 if req.method == "HEAD" else 200)
        http = _http_with_routes(handler)
        self.assertTrue(emailer.validate_url("https://e.example/x", http=http))

    def test_connect_error_invalid(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")
        http = _http_with_routes(handler)
        self.assertFalse(emailer.validate_url("https://e.example/dead", http=http))


class RenderDigestTest(unittest.TestCase):
    def test_html_contains_titles_and_links(self) -> None:
        items = [
            _mk_ranked("a", rank=1, reasoning="Why a matters"),
            _mk_ranked("b", rank=2, reasoning="Why b matters"),
        ]
        html, text = emailer.render_digest(items, digest_date="2026-05-05")
        for r in items:
            self.assertIn(r.story.canonical_title, html)
            self.assertIn(r.story.canonical_url, html)
            self.assertIn(r.reasoning, html)
        self.assertIn("2026-05-05", html)
        self.assertIn("Daily Healthcare Signal", html)
        # Plain text should also have them
        for r in items:
            self.assertIn(r.story.canonical_url, text)

    def test_empty_renders_no_stories_message(self) -> None:
        html, text = emailer.render_digest([], digest_date="2026-05-05")
        self.assertIn("No stories", html)
        self.assertIn("No stories", text)

    def test_html_escapes_titles(self) -> None:
        item = RankedStory(
            story=Story(
                id="x", canonical_url="https://e.example/x",
                canonical_title="<script>alert(1)</script> bad",
                canonical_summary="x", published_at=_utcnow(),
                relevance_score=0.5,
            ),
            rank=1, reasoning="x",
        )
        html, _text = emailer.render_digest([item], digest_date="2026-05-05")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class _SendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._patch_logs = mock.patch.object(
            config, "LOGS_DIR", Path(self.tmp.name),
        )
        self._patch_logs.start()
        self._patch_smtp = mock.patch.multiple(
            config,
            SMTP_HOST="smtp.example.com",
            SMTP_PORT=587,
            SMTP_USER="user@example.com",
            SMTP_PASSWORD="pw",
            SMTP_FROM="user@example.com",
        )
        self._patch_smtp.start()

    def tearDown(self) -> None:
        self._patch_smtp.stop()
        self._patch_logs.stop()
        self.tmp.cleanup()


class SendHappyPathTest(_SendTestBase):
    def test_sends_with_correct_calls(self) -> None:
        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)
        items = [_mk_ranked("a", rank=1, reasoning="a"),
                 _mk_ranked("b", rank=2, reasoning="b")]
        result = emailer.send_digest(
            items, digest_date="2026-05-05",
            recipients=["x@y.com"],
            smtp_factory=factory,
            skip_url_validation=True,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 2)
        self.assertEqual(result.stories_dropped_invalid_url, 0)
        smtp_mock.starttls.assert_called_once()
        smtp_mock.login.assert_called_once_with("user@example.com", "pw")
        smtp_mock.send_message.assert_called_once()
        smtp_mock.quit.assert_called_once()
        # The EmailMessage passed to send_message
        msg_arg = smtp_mock.send_message.call_args[0][0]
        self.assertEqual(msg_arg["Subject"], "Healthcare Signal — 2026-05-05")
        self.assertEqual(msg_arg["From"], "user@example.com")
        self.assertEqual(msg_arg["To"], "x@y.com")


class SendDropsInvalidUrlTest(_SendTestBase):
    def test_invalid_url_dropped(self) -> None:
        good = _mk_ranked("good", rank=1, reasoning="ok")
        bad = _mk_ranked("bad", rank=2, reasoning="should drop")
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if "bad" in str(req.url) else 200)
        http = _http_with_routes(handler)

        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)
        result = emailer.send_digest(
            [good, bad], digest_date="2026-05-05",
            recipients=["x@y.com"],
            smtp_factory=factory, http=http,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 1)
        self.assertEqual(result.stories_dropped_invalid_url, 1)
        # Bad URL should not appear in the rendered email
        self.assertNotIn("https://e.example/bad", result.rendered_html)
        self.assertIn("https://e.example/good", result.rendered_html)


class SendFailureTest(_SendTestBase):
    def test_smtp_exception_returns_error(self) -> None:
        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        smtp_mock.send_message.side_effect = smtplib.SMTPException("auth failed")
        factory = mock.MagicMock(return_value=smtp_mock)
        result = emailer.send_digest(
            [_mk_ranked("a")], digest_date="2026-05-05",
            recipients=["x@y.com"],
            smtp_factory=factory,
            skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertIsNotNone(result.error)
        self.assertIn("SMTPException", result.error)

    def test_no_recipients_short_circuits(self) -> None:
        result = emailer.send_digest(
            [_mk_ranked("a")], digest_date="2026-05-05",
            recipients=[], skip_url_validation=True,
        )
        self.assertFalse(result.sent)
        self.assertEqual(result.error, "no recipients configured")


class SendEmptyDigestTest(_SendTestBase):
    def test_empty_digest_still_sends(self) -> None:
        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)
        result = emailer.send_digest(
            [], digest_date="2026-05-05",
            recipients=["x@y.com"],
            smtp_factory=factory, skip_url_validation=True,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.stories_sent, 0)
        self.assertIn("No stories", result.rendered_html)


class LoggingTest(_SendTestBase):
    def test_log_record_written(self) -> None:
        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)
        emailer.send_digest(
            [_mk_ranked("a")], digest_date="2026-05-05",
            recipients=["x@y.com"],
            smtp_factory=factory, skip_url_validation=True,
        )
        log_files = list(Path(self.tmp.name).glob("emailer_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        rec = json.loads(log_files[0].read_text().strip())
        self.assertTrue(rec["sent"])
        self.assertEqual(rec["digest_date"], "2026-05-05")
        self.assertEqual(rec["stories_sent"], 1)


if __name__ == "__main__":
    unittest.main()
