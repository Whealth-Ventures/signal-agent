"""Smoke tests for src/perplexity_client.py — uses httpx.MockTransport, no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import perplexity_client as pc  # noqa: E402
from query_planner import build_query_plans  # noqa: E402


def _make_canned(json_body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=json_body)


def _ok_payload() -> dict:
    return {
        "choices": [{"message": {"content": "ok"}}],
        "citations": ["https://example.com/a", "https://example.com/b"],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


class CostTest(unittest.TestCase):
    def test_sonar_pro_cost_math(self) -> None:
        # 1000 in @ $3/Mtok = $0.003; 1000 out @ $15/Mtok = $0.015 → $0.018
        cost = pc._estimate_cost("sonar-pro", 1000, 1000)
        self.assertAlmostEqual(cost, 0.018, places=6)

    def test_sonar_reasoning_cost_math(self) -> None:
        # 1000 in @ $1/Mtok + 1000 out @ $5/Mtok = $0.006
        cost = pc._estimate_cost("sonar-reasoning", 1000, 1000)
        self.assertAlmostEqual(cost, 0.006, places=6)

    def test_unknown_model_zero_cost(self) -> None:
        self.assertEqual(pc._estimate_cost("sonar-tiny", 1000, 1000), 0.0)


class _ClientTestBase(unittest.TestCase):
    """Patches LOGS_DIR to a temp dir so tests never touch real data/logs/."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._patch_logs = mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name))
        self._patch_logs.start()

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.tmp.cleanup()

    def _client(self, handler) -> pc.PerplexityClient:
        transport = httpx.MockTransport(handler)
        http = httpx.Client(
            transport=transport,
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        )
        return pc.PerplexityClient(api_key="test", http=http, no_wait_for_tests=True)


class SuccessTest(_ClientTestBase):
    def test_complete_returns_chat_response(self) -> None:
        client = self._client(lambda req: _make_canned(_ok_payload()))
        resp = client.complete("hi", model="sonar-pro", query_id="t1")
        self.assertEqual(resp.text, "ok")
        self.assertEqual(resp.citations, ("https://example.com/a", "https://example.com/b"))
        self.assertEqual(resp.prompt_tokens, 100)
        self.assertEqual(resp.completion_tokens, 50)
        self.assertGreater(resp.estimated_cost_usd, 0)
        self.assertEqual(client.calls_today, 1)
        self.assertEqual(client.remaining_today, config.MAX_PERPLEXITY_CALLS_PER_DAY - 1)

    def test_log_line_written_with_expected_fields(self) -> None:
        client = self._client(lambda req: _make_canned(_ok_payload()))
        client.complete("hi", model="sonar-pro", query_id="t1")
        files = list(Path(self.tmp.name).glob("perplexity_*.jsonl"))
        self.assertEqual(len(files), 1)
        line = files[0].read_text().strip()
        rec = json.loads(line)
        for k in (
            "ts", "model", "query_id", "status", "latency_ms",
            "prompt_tokens", "completion_tokens", "citations", "cost_usd", "attempts",
        ):
            self.assertIn(k, rec)
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["query_id"], "t1")
        self.assertEqual(rec["model"], "sonar-pro")
        self.assertEqual(rec["citations"], 2)


class RetryTest(_ClientTestBase):
    def test_429_then_200(self) -> None:
        responses = iter([
            httpx.Response(429, text="rate limited"),
            _make_canned(_ok_payload()),
        ])
        client = self._client(lambda req: next(responses))
        resp = client.complete("hi", query_id="retry")
        self.assertEqual(resp.text, "ok")

    def test_500_then_200(self) -> None:
        responses = iter([
            httpx.Response(500, text="server error"),
            _make_canned(_ok_payload()),
        ])
        client = self._client(lambda req: next(responses))
        resp = client.complete("hi", query_id="retry")
        self.assertEqual(resp.text, "ok")

    def test_401_does_not_retry(self) -> None:
        n = [0]
        def handler(req):
            n[0] += 1
            return httpx.Response(401, text="unauthorized")
        client = self._client(handler)
        with self.assertRaises(pc.PerplexityCallFailed):
            client.complete("hi", query_id="auth")
        self.assertEqual(n[0], 1)

    def test_terminal_failure_logs_error(self) -> None:
        client = self._client(lambda req: httpx.Response(401, text="unauth"))
        with self.assertRaises(pc.PerplexityCallFailed):
            client.complete("hi", query_id="auth")
        files = list(Path(self.tmp.name).glob("perplexity_*.jsonl"))
        rec = json.loads(files[0].read_text().strip())
        self.assertEqual(rec["status"], 401)
        self.assertIn("error", rec)


class RateLimitTest(_ClientTestBase):
    def test_at_cap_raises_before_http(self) -> None:
        # Pre-populate today's log with N=cap successful calls.
        log = Path(self.tmp.name) / f"perplexity_{pc._today_str()}.jsonl"
        with log.open("w") as f:
            for _ in range(config.MAX_PERPLEXITY_CALLS_PER_DAY):
                f.write(json.dumps({"status": 200}) + "\n")

        n = [0]
        def handler(req):
            n[0] += 1
            return _make_canned(_ok_payload())
        client = self._client(handler)
        with self.assertRaises(pc.RateLimitExceeded):
            client.complete("hi")
        self.assertEqual(n[0], 0)  # no HTTP call attempted

    def test_below_cap_works(self) -> None:
        log = Path(self.tmp.name) / f"perplexity_{pc._today_str()}.jsonl"
        with log.open("w") as f:
            for _ in range(config.MAX_PERPLEXITY_CALLS_PER_DAY - 1):
                f.write(json.dumps({"status": 200}) + "\n")
        client = self._client(lambda req: _make_canned(_ok_payload()))
        client.complete("hi", query_id="last_one")  # 60th and final call
        with self.assertRaises(pc.RateLimitExceeded):
            client.complete("hi", query_id="over")


class SearchRecentTest(_ClientTestBase):
    def test_uses_sonar_pro_and_day_recency_with_plan_prompt(self) -> None:
        captured: dict = {}
        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content.decode())
            captured["url"] = str(req.url)
            return _make_canned(_ok_payload())
        client = self._client(handler)

        plan = build_query_plans()[0]
        client.search_recent(plan)
        body = captured["body"]
        self.assertEqual(body["model"], "sonar-pro")
        self.assertEqual(body["search_recency_filter"], "day")
        self.assertEqual(captured["url"], pc.PERPLEXITY_URL)
        # The plan's prompt_text should be in the user message.
        user_msgs = [m for m in body["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], plan.prompt_text)


@unittest.skipUnless(os.getenv("RUN_LIVE") == "1", "set RUN_LIVE=1 to hit the real API (uses 1 of 60 daily calls)")
class LiveSmokeTest(unittest.TestCase):
    """Opt-in live test against the real Perplexity API."""

    def test_live_ping(self) -> None:
        config.check_env()
        client = pc.PerplexityClient()
        resp = client.complete(
            "Reply with the single word: pong.",
            model="sonar-pro",
            query_id="live_smoke",
        )
        self.assertTrue(resp.text)
        self.assertGreaterEqual(resp.prompt_tokens, 1)


if __name__ == "__main__":
    unittest.main()
