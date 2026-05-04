"""Smoke tests for src/main.py — fully offline end-to-end."""
from __future__ import annotations

import hashlib
import json
import smtplib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import chromadb
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import main as main_module  # noqa: E402
import storage  # noqa: E402
from models import story_id  # noqa: E402
from perplexity_client import ChatResponse, RateLimitExceeded  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Stubs --------------------------------------------------------------

class FakePerplexityClient:
    """Cap-aware fake. .complete() and .search_recent() both count toward cap."""

    def __init__(self, response_per_plan: dict[str, dict] | None = None,
                 ranker_response: dict | None = None, cap: int = 60) -> None:
        self.response_per_plan = response_per_plan or {}
        self.ranker_response = ranker_response or {"ranked": []}
        self._calls = 0
        self._cap = cap

    @property
    def calls_today(self) -> int:
        return self._calls

    @property
    def remaining_today(self) -> int:
        return max(0, self._cap - self._calls)

    def search_recent(self, plan):
        if self._calls >= self._cap:
            raise RateLimitExceeded(f"cap: {self._calls}/{self._cap}")
        self._calls += 1
        body = self.response_per_plan.get(plan.id, {"stories": []})
        return ChatResponse(
            text=json.dumps(body), citations=(), model="sonar-pro",
            prompt_tokens=100, completion_tokens=50,
            estimated_cost_usd=0.005, raw={},
        )

    def complete(self, prompt: str, *, model: str = "", recency=None,
                 query_id: str = "", system=None) -> ChatResponse:
        if self._calls >= self._cap:
            raise RateLimitExceeded(f"cap: {self._calls}/{self._cap}")
        self._calls += 1
        return ChatResponse(
            text=json.dumps(self.ranker_response), citations=(), model=model,
            prompt_tokens=100, completion_tokens=50,
            estimated_cost_usd=0.005, raw={},
        )


def _stub_embedder(dim: int = 8):
    def embed(texts: list[str]) -> tuple[list[list[float]], int]:
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            # Signed vectors in [-1, 1] so unrelated hashes can be orthogonal —
            # all-positive components inflate cosine similarity between
            # arbitrary texts and break clustering.
            v = [(h[i % len(h)] / 127.5) - 1.0 for i in range(dim)]
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out, max(1, sum(len(t) for t in texts) // 4)
    return embed


# --- Parser tests -------------------------------------------------------

class _PlanStub:
    def __init__(self, pid: str = "p1", bucket: str = "Bucket"):
        self.id = pid
        self.bucket = bucket


class ParseResponseTest(unittest.TestCase):
    def _resp(self, text: str, citations: tuple = ()) -> ChatResponse:
        return ChatResponse(
            text=text, citations=citations, model="sonar-pro",
            prompt_tokens=10, completion_tokens=5,
            estimated_cost_usd=0.0, raw={},
        )

    def test_clean_json(self) -> None:
        plan = _PlanStub(pid="india__1", bucket="Care Delivery")
        body = {"stories": [
            {"title": "Acme raises", "url": "https://e.example/a",
             "published": "2026-05-05T10:00:00Z",
             "summary": "Funding round news."},
        ]}
        signals = main_module.parse_perplexity_response(plan, self._resp(json.dumps(body)))
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].title, "Acme raises")
        self.assertEqual(signals[0].url, "https://e.example/a")
        self.assertEqual(signals[0].source_type, "perplexity")

    def test_fenced_json(self) -> None:
        plan = _PlanStub()
        body = '```json\n{"stories": [{"title": "T", "url": "https://e.example/x", "published": null, "summary": "s"}]}\n```'
        signals = main_module.parse_perplexity_response(plan, self._resp(body))
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].url, "https://e.example/x")

    def test_garbage_uses_citation_fallback(self) -> None:
        plan = _PlanStub()
        signals = main_module.parse_perplexity_response(
            plan, self._resp("not json at all", citations=("https://a.example", "https://b.example")),
        )
        self.assertEqual(len(signals), 2)
        for s in signals:
            self.assertTrue(s.raw.get("fallback"))

    def test_garbage_no_citations_returns_empty(self) -> None:
        plan = _PlanStub()
        signals = main_module.parse_perplexity_response(plan, self._resp("nope"))
        self.assertEqual(signals, [])


class FetchPerplexityCapTest(unittest.TestCase):
    def test_short_circuits_when_cap_at_headroom(self) -> None:
        # remaining_today = 2 == headroom → loop should break before any call
        client = FakePerplexityClient(cap=main_module.PERPLEXITY_HEADROOM)
        # cap=2, _calls=0 → remaining=2; first iteration: remaining(2) <= headroom(2) → break
        from query_planner import build_query_plans
        plans = build_query_plans()
        sigs = main_module.fetch_perplexity(client, plans)
        self.assertEqual(client.calls_today, 0)
        self.assertEqual(sigs, [])


# --- ensure_content_indexed --------------------------------------------

class EnsureIndexedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._patch_logs = mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name))
        self._patch_logs.start()

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.tmp.cleanup()

    def test_auto_indexes_when_empty(self) -> None:
        content_dir = Path(self.tmp.name) / "content"
        (content_dir / "articles").mkdir(parents=True)
        (content_dir / "articles" / "x.md").write_text("# X\n\nBody of X.")
        chroma = chromadb.PersistentClient(path=str(Path(self.tmp.name) / "chroma"))
        coll = chroma.get_or_create_collection(name="content_corpus",
                                               metadata={"hnsw:space": "cosine"})
        self.assertEqual(coll.count(), 0)

        main_module.ensure_content_indexed(
            chroma_client=chroma,
            embedder=_stub_embedder(),
            content_dir=content_dir,
        )

        coll = chroma.get_collection("content_corpus")
        self.assertGreater(coll.count(), 0)


# --- End-to-end pipeline -----------------------------------------------

class _PipelineBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = storage.connect(self.db_path)
        storage.init_db(conn=self.conn)
        self.conn.commit()

        self._patches = [
            mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name)),
            mock.patch.object(config, "DIGEST_RECIPIENTS", ("test@example.com",)),
            mock.patch.object(config, "SMTP_HOST", "smtp.example.com"),
            mock.patch.object(config, "SMTP_PORT", 587),
            mock.patch.object(config, "SMTP_USER", "user@example.com"),
            mock.patch.object(config, "SMTP_PASSWORD", "pw"),
            mock.patch.object(config, "SMTP_FROM", "user@example.com"),
        ]
        for p in self._patches:
            p.start()

        self.chroma = chromadb.PersistentClient(path=str(Path(self.tmp.name) / "chroma"))
        # MockTransport returning 404 — RSS finds no feeds → 0 RSS signals
        self.http = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(404, text="not found"),
        ))

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.http.close()
        self.conn.close()
        self.tmp.cleanup()


class HappyPathTest(_PipelineBase):
    def test_end_to_end(self) -> None:
        # Canned Perplexity responses for 2 plans
        responses = {
            "india__1_care_delivery_models": {"stories": [
                {"title": "Acme raises Series B",
                 "url": "https://e.example/acme",
                 "published": "2026-05-05T10:00:00Z",
                 "summary": "Big healthcare funding round."},
            ]},
            "us__1_care_delivery_models": {"stories": [
                {"title": "Hospital opens new wing",
                 "url": "https://e.example/wing",
                 "published": "2026-05-05T08:00:00Z",
                 "summary": "Local healthcare news."},
            ]},
        }
        ranker_resp = {"ranked": [
            {"story_id": story_id("https://e.example/acme"),
             "rank": 1, "reasoning": "Most relevant funding"},
            {"story_id": story_id("https://e.example/wing"),
             "rank": 2, "reasoning": "Local development"},
        ]}
        client = FakePerplexityClient(
            response_per_plan=responses, ranker_response=ranker_resp,
        )

        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)

        stats = main_module.run_pipeline(
            digest_date="2026-05-05",
            conn=self.conn, chroma_client=self.chroma,
            perplexity_client=client, smtp_factory=factory,
            embedder=_stub_embedder(), http_client=self.http,
            skip_url_validation=True, skip_content_indexing=True,
        )
        self.conn.commit()

        # Counts
        self.assertEqual(stats.perplexity_signals, 2)
        self.assertEqual(stats.rss_signals, 0)
        self.assertEqual(stats.signals_saved, 2)
        self.assertEqual(stats.stories_created, 2)
        self.assertGreaterEqual(stats.ranked_count, 2)
        self.assertTrue(stats.digest_sent)

        # SMTP was called
        smtp_mock.send_message.assert_called_once()

        # Digest persisted with status='sent'
        rows = self.conn.execute("SELECT status FROM digests").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "sent")

        # digest_stories has 2 rows
        ds = self.conn.execute("SELECT count(*) AS c FROM digest_stories").fetchone()
        self.assertEqual(ds["c"], 2)


class SmtpFailureTest(_PipelineBase):
    def test_marks_digest_failed_and_returns_false(self) -> None:
        responses = {
            "india__1_care_delivery_models": {"stories": [
                {"title": "Acme raises Series B",
                 "url": "https://e.example/acme",
                 "published": "2026-05-05T10:00:00Z",
                 "summary": "Funding."},
            ]},
        }
        ranker_resp = {"ranked": [
            {"story_id": story_id("https://e.example/acme"), "rank": 1, "reasoning": "x"},
        ]}
        client = FakePerplexityClient(response_per_plan=responses,
                                       ranker_response=ranker_resp)

        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        smtp_mock.send_message.side_effect = smtplib.SMTPException("auth failed")
        factory = mock.MagicMock(return_value=smtp_mock)

        stats = main_module.run_pipeline(
            digest_date="2026-05-05",
            conn=self.conn, chroma_client=self.chroma,
            perplexity_client=client, smtp_factory=factory,
            embedder=_stub_embedder(), http_client=self.http,
            skip_url_validation=True, skip_content_indexing=True,
        )
        self.conn.commit()

        self.assertFalse(stats.digest_sent)
        rows = self.conn.execute(
            "SELECT status, error FROM digests"
        ).fetchall()
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("SMTPException", rows[0]["error"] or "")


class CapHitTest(_PipelineBase):
    def test_pipeline_still_completes_when_perplexity_capped(self) -> None:
        # cap = headroom → fetch_perplexity yields 0 signals, ranker also blocked
        client = FakePerplexityClient(cap=main_module.PERPLEXITY_HEADROOM)
        smtp_mock = mock.MagicMock(spec=smtplib.SMTP)
        factory = mock.MagicMock(return_value=smtp_mock)

        stats = main_module.run_pipeline(
            digest_date="2026-05-05",
            conn=self.conn, chroma_client=self.chroma,
            perplexity_client=client, smtp_factory=factory,
            embedder=_stub_embedder(), http_client=self.http,
            skip_url_validation=True, skip_content_indexing=True,
        )
        self.conn.commit()

        self.assertEqual(stats.perplexity_signals, 0)
        self.assertEqual(stats.signals_saved, 0)
        self.assertEqual(stats.stories_created, 0)
        # Ranker should be the short-circuit path (0 candidates) and not call complete()
        self.assertEqual(client.calls_today, 0)
        self.assertTrue(stats.digest_sent)  # empty digest still ships


if __name__ == "__main__":
    unittest.main()
