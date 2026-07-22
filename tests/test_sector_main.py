"""Wiring test for src/sector_main.py — proves the isolation guarantee.

Mocks the three network seams (Perplexity fetch, scorer embeddings/Chroma, and
the Slack post) so it runs offline, then asserts the Sector Agent writes its
digest to `sector.db` and leaves the daily `agent.db` completely untouched — the
core reason the sector pipeline is a separate entrypoint with its own DB.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import sector  # noqa: E402
import sector_main  # noqa: E402
import storage  # noqa: E402
from models import Story, story_id  # noqa: E402
from perplexity_client import ChatResponse  # noqa: E402
from slack_client import SlackResult  # noqa: E402


class _FakePerp:
    """Sync-only fake → run() takes the fetch_perplexity (sync) branch. Its
    search_recent has the legacy (plan) signature, so recency stays optional."""
    def __init__(self, *a, **k):
        self._calls = 0

    @property
    def calls_today(self) -> int:
        return self._calls

    @property
    def remaining_today(self) -> int:
        return 100

    def search_recent(self, plan, *, recency=None):
        # Sector path passes recency="week"; the real client accepts it too.
        self._calls += 1
        body = {"stories": [
            {"title": f"News affecting {plan.bucket}", "url": f"https://e.example/{plan.id}",
             "published": None, "summary": "s"},
        ]}
        return ChatResponse(text=json.dumps(body), citations=(), model="m",
                            prompt_tokens=1, completion_tokens=1,
                            estimated_cost_usd=0.001, raw={})


class _FakeRankClient:
    # Empty items → rank_impact falls back to grouping by surfacing company.
    def complete(self, prompt, **kw):
        return ChatResponse(text='{"items": []}', citations=(), model="m",
                            prompt_tokens=1, completion_tokens=1,
                            estimated_cost_usd=0.0, raw={})


def _fake_scoring_factory(company_names: list[str]):
    """Return a run_scoring stand-in that inserts one story per company (bucket
    = company name) into the passed connection — no embeddings, no Chroma."""
    def fake_scoring(*, conn=None, **kw):
        for i, company in enumerate(company_names):
            url = f"https://e.example/story{i}"
            storage.upsert_story(
                Story(id=story_id(url), canonical_url=url, canonical_title=f"t{i}",
                      canonical_summary="s",
                      published_at=datetime.now(timezone.utc),
                      relevance_score=0.5, bucket=company),
                conn=conn,
            )
        conn.commit()
        return type("S", (), {"stories_created": len(company_names)})()
    return fake_scoring


class SectorMainWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.sector_db = d / "sector.db"
        self.agent_db = d / "agent.db"
        cos = sector.load_portfolio()
        self.companies = [cos[0].name, cos[1].name]
        self._patches = [
            mock.patch.object(config, "SECTOR_DB_PATH", self.sector_db),
            mock.patch.object(config, "DB_PATH", self.agent_db),
            mock.patch.object(config, "SLACK_CHANNEL_ID_SECTOR", "C_SECTOR"),
            mock.patch.object(sector_main, "PerplexityClient", _FakePerp),
            mock.patch.object(sector_main.scorer, "run_scoring",
                              _fake_scoring_factory(self.companies)),
            mock.patch.object(sector.ranker, "_build_ranker_client",
                              lambda: (_FakeRankClient(), "model")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_writes_sector_db_and_leaves_agent_db_untouched(self) -> None:
        sent_ok = SlackResult(
            sent=True, channel_label="Signal Agent Sector", stories_sent=2,
            stories_dropped_invalid_url=0, elapsed_seconds=0.0,
            slack_ts="1.1", slack_channel="C_SECTOR",
        )
        with mock.patch.object(sector_main.sector, "post_sector_digest",
                               return_value=sent_ok):
            ok = sector_main.run(
                digest_date="2026-07-27", skip_content_indexing=True,
            )
        self.assertTrue(ok)

        # Sector db has the sent digest, per its own channel.
        sconn = storage.connect(self.sector_db)
        try:
            self.assertTrue(storage.has_sent_digest_for_date(
                "2026-07-27", slack_channel="C_SECTOR", conn=sconn))
            self.assertEqual(len(storage.list_stories(conn=sconn)), 2)
        finally:
            sconn.close()

        # The daily agent.db was never even created by the sector run.
        self.assertFalse(
            self.agent_db.exists(),
            "sector run must not touch the daily agent.db",
        )

    def test_dry_run_creates_no_digest(self) -> None:
        ok = sector_main.run(
            digest_date="2026-07-27", skip_content_indexing=True, dry_run=True,
        )
        self.assertTrue(ok)
        out = config.LOGS_DIR / "dry_run_sector_2026-07-27.json"
        self.assertTrue(out.exists())
        payload = json.loads(out.read_text())
        self.assertIn("blocks", payload)
        # No digest row written on a dry run.
        sconn = storage.connect(self.sector_db)
        try:
            self.assertFalse(storage.has_sent_digest_for_date(
                "2026-07-27", slack_channel="C_SECTOR", conn=sconn))
        finally:
            sconn.close()


if __name__ == "__main__":
    unittest.main()
