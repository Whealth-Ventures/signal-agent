"""Tests for src/enricher.py — body fetch, tagging, validation, and retry-by-
next-run. No network: the fetcher and the OpenAI client are faked."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import enricher  # noqa: E402
import storage  # noqa: E402
from models import Story  # noqa: E402

_TS = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _story(sid: str) -> Story:
    return Story(
        id=sid, canonical_url=f"https://ex.com/{sid}", canonical_title=f"title {sid}",
        canonical_summary=f"summary {sid}: hospital news", published_at=_TS, relevance_score=0.5,
    )


class _FakeClient:
    """chat.completions.create → canned JSON; `fail=True` raises instead."""
    def __init__(self, entries: list[dict], fail: bool = False):
        self.entries, self.fail, self.calls = entries, fail, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        sent = {s["id"] for s in json.loads(kwargs["messages"][1]["content"])["stories"]}
        out = [e for e in self.entries if e["id"] in sent or e["id"] == "invented"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"stories": out})))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


GOOD = {
    "id": "a", "healthcare": True, "category": "venture_ipo", "magnitude": "S",
    "facts": {"event": "ipo", "amount": {"value": 218, "unit": "crore", "currency": "INR"},
              "exchange": "NSE", "indication": "not a venture_ipo field"},
    "companies": ["Acme Health"], "geo": "India",
    "summary": "Acme Health opens its IPO. It seeks $26M on the NSE.",
}
MESSY = {
    "id": "b", "healthcare": True, "category": "ai_healthcare", "magnitude": "Z",
    "facts": {"event": "rumour", "use_case": "clinical_documentation",
              "amount_usd": 5e7, "customers": "Mayo"},
    "companies": "Acme", "geo": "Mars", "summary": "",
}


class EnricherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = storage.connect(Path(self.tmp.name) / "t.db")
        storage.init_db(conn=self.conn)
        for sid in ("a", "b", "old"):
            storage.upsert_story(_story(sid), conn=self.conn)
        # 'old' was first seen 10 days ago: outside the default 2-day window.
        self.conn.execute(
            "UPDATE stories SET created_at = ? WHERE id = 'old'",
            ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),),
        )
        self.conn.commit()
        self.fetched: list[list[str]] = []
        patcher = mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def _fetch(self, urls):
        self.fetched.append(sorted(urls))
        return {"https://ex.com/a": "Full body of a hospital story. " * 12}

    def _details(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM story_details").fetchall()
        return {r["story_id"]: dict(r) for r in rows}

    def test_fetches_tags_and_validates_within_window(self):
        client = _FakeClient([GOOD, MESSY, {**GOOD, "id": "invented"}])
        stats = enricher.run(days=2, conn=self.conn, client=client, fetch=self._fetch)

        self.assertEqual(self.fetched, [["https://ex.com/a", "https://ex.com/b"]])
        d = self._details()
        self.assertEqual(set(d), {"a", "b"})  # 'old' skipped, 'invented' ignored
        self.assertEqual((d["a"]["body_status"], d["b"]["body_status"]), ("ok", "failed"))
        self.assertEqual(
            (d["a"]["category"], d["a"]["magnitude"], d["a"]["geo"]), ("venture_ipo", "S", "India"),
        )
        # Only the story's own category's fields survive.
        self.assertEqual(
            json.loads(d["a"]["facts"]),
            {"event": "ipo", "amount_usd": 218e7 / enricher.PER_USD["INR"], "amount_text": "\u20b9218 Cr",
             "exchange": "NSE"},
        )
        self.assertEqual(json.loads(d["a"]["companies"]), ["Acme Health"])
        # Off-list values fall back to safe defaults instead of failing the
        # story; an AI story never keeps a deal size.
        self.assertEqual(json.loads(d["b"]["facts"]), {"use_case": "clinical_documentation"})
        self.assertEqual(
            (d["b"]["magnitude"], json.loads(d["b"]["companies"]), d["b"]["geo"], d["b"]["summary"]),
            (None, [], None, None),
        )
        self.assertEqual((stats["tagged"], stats["failed_calls"]), (2, 0))

    def test_failed_call_keeps_bodies_and_next_run_tags_without_refetch(self):
        first = enricher.run(
            days=2, conn=self.conn, client=_FakeClient([], fail=True), fetch=self._fetch,
        )
        self.assertEqual((first["tagged"], first["failed_calls"]), (0, 1))
        self.assertTrue(all(r["tagged_at"] is None for r in self._details().values()))

        second = enricher.run(
            days=2, conn=self.conn, client=_FakeClient([GOOD, MESSY]), fetch=self._fetch,
        )
        self.assertEqual(len(self.fetched), 1)  # bodies were not fetched again
        self.assertEqual(second["tagged"], 2)
        self.assertTrue(all(r["tagged_at"] for r in self._details().values()))

    def test_failed_fetch_is_judged_by_the_model_not_the_lexicon(self):
        # No healthcare stem in title or summary, and no body: the lexicon would
        # veto it, but a failed fetch leaves too little text to judge.
        storage.upsert_story(Story(
            id="u", canonical_url="https://ex.com/u", canonical_title="Ultrahuman raises $12M Series B",
            canonical_summary="", published_at=_TS, relevance_score=0.5,
        ), conn=self.conn)
        self.conn.commit()
        entry = {"id": "u", "healthcare": True, "category": "venture_ipo", "magnitude": "A",
                 "facts": {"event": "funding_round", "amount_usd": 1.2e7}}
        enricher.run(days=2, conn=self.conn, client=_FakeClient([GOOD, MESSY, entry]), fetch=self._fetch)
        u = self._details()["u"]
        self.assertEqual((u["body_status"], u["category"], u["magnitude"]), ("failed", "venture_ipo", "A"))

    def test_junk_bodies_count_as_a_failed_fetch(self):
        # A JS wall or a PDF read as text is "fetched" but says nothing: stored
        # as failed, so the lexicon can't veto on it and the model decides.
        storage.upsert_story(Story(
            id="u", canonical_url="https://ex.com/u", canonical_title="Ultrahuman raises $12M Series B",
            canonical_summary="", published_at=_TS, relevance_score=0.5,
        ), conn=self.conn)
        self.conn.commit()
        junk = {"https://ex.com/u": "Please enable JavaScript to continue.",
                "https://ex.com/a": "%PDF-1.7 " + "x" * 400}
        entry = {"id": "u", "healthcare": True, "category": "venture_ipo", "magnitude": "A", "facts": {}}
        enricher.run(days=2, conn=self.conn, client=_FakeClient([GOOD, MESSY, entry]),
                     fetch=lambda urls: junk)
        d = self._details()
        self.assertEqual((d["u"]["body_status"], d["u"]["category"]), ("failed", "venture_ipo"))
        self.assertEqual((d["a"]["body_status"], d["a"]["body"]), ("failed", None))

    def test_an_untagged_story_is_retried_after_the_fetch_window(self):
        # Fetched 10 days ago, never tagged (say, a long OpenAI outage).
        storage.save_story_body("old", "Hospital body.", conn=self.conn)
        self.conn.commit()
        entry = {**GOOD, "id": "old"}
        enricher.run(days=2, conn=self.conn, client=_FakeClient([GOOD, MESSY, entry]), fetch=self._fetch)
        self.assertEqual(self._details()["old"]["category"], "venture_ipo")
        self.assertNotIn("https://ex.com/old", self.fetched[0])  # retried, not re-fetched

    def test_money_is_converted_in_code_not_by_the_model(self):
        m = enricher.parse_money
        self.assertEqual(m({"value": 4800, "unit": "crore", "currency": "INR"}),
                         (4.8e10 / enricher.PER_USD["INR"], "\u20b94,800 Cr"))
        self.assertEqual(m({"value": 446.3, "unit": "million", "currency": "usd"}), (446.3e6, "$446.3M"))
        self.assertEqual(m({"value": 50, "unit": "Million", "currency": "SGD"})[1], "SGD 50M")
        # A units slip ($350M as 350), unknown unit or currency, or junk → no amount.
        for bad in ({"value": 350, "unit": "", "currency": "USD"}, {"value": 5, "unit": "gazillion", "currency": "USD"},
                    {"value": 5, "unit": "million", "currency": "XYZ"}, {"value": True, "unit": "million", "currency": "USD"},
                    350, None):
            self.assertIsNone(m(bad), bad)
        t = enricher._clean({"healthcare": True, "category": "venture_ipo",
                             "facts": {"event": "ipo", "amount": {"value": 350, "unit": "", "currency": "USD"}}}, None)
        self.assertEqual(t["facts"], {"event": "ipo"})

    def test_company_names_are_capped(self):
        t = enricher._clean({"healthcare": True, "companies": ["x" * 500]}, None)
        self.assertEqual(len(t["companies"][0]), 120)

    def test_every_category_has_fields_and_the_prompt_lists_them(self):
        prompt = enricher._system_prompt()
        for b in config.PRIORITY_BUCKETS:
            self.assertIn(b.key, enricher.CATEGORY_FIELDS, f"{b.key} has no fields")
            for name, (kind, _) in enricher.CATEGORY_FIELDS[b.key].items():
                self.assertIn(name, prompt)
                for v in kind if isinstance(kind, tuple) else ():
                    self.assertIn(v, prompt)

    def test_unknown_category_becomes_other_healthcare_with_no_facts(self):
        t = enricher._clean(
            {"healthcare": True, "category": "crypto", "facts": {"event": "ipo"}},
            "A hospital chain lists",
        )
        self.assertEqual((t["category"], t["facts"]), ("other_healthcare", {}))

    def test_non_healthcare_is_tier_c_with_no_facts_whoever_says_so(self):
        ipo = {"category": "venture_ipo", "magnitude": "S",
               "facts": {"event": "ipo", "amount_usd": 2.6e9}}
        # The model says no...
        t = enricher._clean({**ipo, "healthcare": False}, "A hospital chain lists")
        self.assertEqual((t["category"], t["magnitude"], t["facts"]), ("not_healthcare", "C", {}))
        # ...or the model says yes but the digest's lexicon gate finds no healthcare.
        t = enricher._clean({**ipo, "healthcare": True}, "NSE lists at a 0.84% premium")
        self.assertEqual((t["category"], t["magnitude"], t["facts"]), ("not_healthcare", "C", {}))


if __name__ == "__main__":
    unittest.main()
