"""Tests for src/alerts.py and its three call sites — no network: Slack
posting and the pipelines are faked."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx
import openai

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import alerts  # noqa: E402
import config  # noqa: E402
import enricher  # noqa: E402
import main  # noqa: E402
import sector_main  # noqa: E402

_REQ = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


def _err(cls, status: int, code: str):
    return cls("boom", response=httpx.Response(status, request=_REQ), body={"code": code})


BILLING = _err(openai.RateLimitError, 429, "insufficient_quota")


class ClassifyTest(unittest.TestCase):
    def test_unfixable_openai_errors(self):
        self.assertEqual(alerts.openai_problem(BILLING), "billing")
        self.assertEqual(alerts.openai_problem(_err(openai.AuthenticationError, 401, "invalid_api_key")), "auth")
        self.assertEqual(alerts.openai_problem(_err(openai.PermissionDeniedError, 403, "x")), "auth")
        self.assertEqual(alerts.openai_problem(_err(openai.NotFoundError, 404, "model_not_found")), "model")

    def test_transient_or_unrelated_errors_do_not_alert(self):
        self.assertIsNone(alerts.openai_problem(_err(openai.RateLimitError, 429, "rate_limit_exceeded")))
        self.assertIsNone(alerts.openai_problem(RuntimeError("disk full")))
        self.assertIsNone(alerts.openai_problem(None))

    def test_finds_the_openai_error_behind_a_wrapper(self):
        try:
            try:
                raise BILLING
            except openai.RateLimitError:
                raise RuntimeError("scoring failed")
        except RuntimeError as wrapped:
            self.assertEqual(alerts.openai_problem(wrapped), "billing")


class PostTest(unittest.TestCase):
    def test_posts_to_the_channel_with_the_fix(self):
        with mock.patch.object(config, "SLACK_BOT_TOKEN", "xoxb-test"), \
             mock.patch.object(alerts.slack_client, "_log"), \
             mock.patch.object(alerts.slack_client, "_post_via_api",
                               return_value=(True, 200, None, "1.2", "C1")) as post:
            ok = alerts.post_openai_alert("billing", impact="Today's digest could not be built.",
                                          channel_id="C_INDIA")
        self.assertTrue(ok)
        kw = post.call_args.kwargs
        self.assertEqual(kw["channel_id"], "C_INDIA")
        self.assertIn("out of credits", kw["text"])
        self.assertIn(alerts.BILLING_URL, kw["text"])
        self.assertNotIn("—", kw["text"])  # no em-dashes in team-facing text

    def test_a_failed_post_never_raises(self):
        with mock.patch.object(config, "SLACK_BOT_TOKEN", "xoxb-test"), \
             mock.patch.object(alerts.slack_client, "_log", side_effect=OSError), \
             mock.patch.object(alerts.slack_client, "_post_via_api", side_effect=RuntimeError):
            self.assertFalse(alerts.post_openai_alert("auth", impact="x", channel_id="C1"))


class CallSiteTest(unittest.TestCase):
    def test_digest_crash_alerts_its_channel_and_reraises(self):
        with mock.patch.object(config, "check_env"), \
             mock.patch.object(config, "SLACK_CHANNEL_ID_US", "C_US"), \
             mock.patch.object(main, "run_pipeline", side_effect=BILLING), \
             mock.patch.object(alerts, "post_openai_alert") as post:
            with self.assertRaises(openai.RateLimitError):
                main.main(["--geo", "us"])
        post.assert_called_once()
        self.assertEqual((post.call_args.args[0], post.call_args.kwargs["channel_id"]), ("billing", "C_US"))

    def test_dry_run_crash_does_not_alert(self):
        with mock.patch.object(config, "check_env"), \
             mock.patch.object(main, "run_pipeline", side_effect=BILLING), \
             mock.patch.object(alerts, "post_openai_alert") as post:
            with self.assertRaises(openai.RateLimitError):
                main.main(["--geo", "india", "--dry-run"])
        post.assert_not_called()

    def test_sector_crash_alerts_the_sector_channel(self):
        with mock.patch.object(config, "check_env"), \
             mock.patch.object(config, "SLACK_CHANNEL_ID_SECTOR", "C_SEC"), \
             mock.patch.object(config, "PORTFOLIO_XLSX", mock.Mock(exists=lambda: True)), \
             mock.patch.object(sector_main, "run", side_effect=BILLING), \
             mock.patch.object(alerts, "post_openai_alert") as post:
            with self.assertRaises(openai.RateLimitError):
                sector_main.main([])
        self.assertEqual(post.call_args.kwargs["channel_id"], "C_SEC")

    def test_enricher_billing_failure_alerts_once_after_the_run(self):
        stats = {"to_tag": 30, "tagged": 0, "openai_problem": "billing"}
        with mock.patch.object(config, "OPENAI_API_KEY", "sk-test"), \
             mock.patch.object(config, "SLACK_CHANNEL_ID_INDIA", "C_IN"), \
             mock.patch.object(enricher.storage, "connect"), \
             mock.patch.object(enricher.storage, "init_db"), \
             mock.patch.object(enricher, "run", return_value=stats), \
             mock.patch.object(alerts, "post_openai_alert") as post:
            self.assertEqual(enricher.main(["--geo", "india"]), 1)
        post.assert_called_once()
        self.assertEqual((post.call_args.args[0], post.call_args.kwargs["channel_id"]), ("billing", "C_IN"))

    def test_tag_batch_reports_the_problem(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = BILLING
        _, usage = enricher._tag_batch(client, "sys", [{"id": "a", "title": "t", "summary": "", "body": ""}])
        self.assertEqual(usage["problem"], "billing")


if __name__ == "__main__":
    unittest.main()
