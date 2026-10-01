"""Tests for src/qa.py (search, tool loop, link guard) and src/bot.py's Slack
handling. No network: OpenAI and Slack are faked; the archive is a tmp DB."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import bot  # noqa: E402
import config  # noqa: E402
import qa  # noqa: E402
import storage  # noqa: E402
from models import Story  # noqa: E402

# id, title, published, category, magnitude, geo, facts, companies
STORIES = [
    ("adarx", "ADARx raises $535M in Nasdaq IPO", "2026-09-25", "venture_ipo", "S", "US",
     {"event": "ipo", "amount_usd": 5.35e8}, ["ADARx Pharmaceuticals"]),
    ("eclat", "Eclat Health weighs $300M India IPO", "2026-09-15", "venture_ipo", "S", "India",
     {"event": "ipo_filing", "amount_usd": 3e8}, ["Eclat Health Solutions"]),
    ("seed", "Ayu raises $2M seed", "2026-09-20", "venture_ipo", "B", "India",
     {"event": "funding_round", "amount_usd": 2e6}, ["Ayu Health"]),
    ("old", "Old IPO from August", "2026-08-20", "venture_ipo", "A", "US",
     {"event": "ipo", "amount_usd": 9e8}, ["OldCo"]),
    ("nse", "NSE lists at a premium", "2026-09-24", "not_healthcare", "C", "India", {}, ["NSE"]),
    ("fda", "FDA approves lirafugratinib", "2026-09-22", "fda_regulatory", "S", "US",
     {"event": "approval", "product": "lirafugratinib"}, ["HLB"]),
]


def _add(conn, sid, title, pub, cat, facts, comps, emb, mag="S"):
    storage.upsert_story(Story(
        id=sid, canonical_url=f"https://ex.com/{sid}", canonical_title=title, canonical_summary="",
        relevance_score=0.5, published_at=datetime.fromisoformat(pub).replace(tzinfo=timezone.utc),
    ), embedding=emb, conn=conn)
    storage.save_story_body(sid, "x" * 400, conn=conn)
    storage.save_story_tags(sid, {"category": cat, "facts": facts, "magnitude": mag, "companies": comps,
                                  "geo": "US", "summary": "s"}, model="test", conn=conn)
    conn.commit()


class _Archive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "agent.db"
        conn = storage.connect(db)
        storage.init_db(conn=conn)
        for sid, title, pub, cat, mag, geo, facts, comps in STORIES:
            storage.upsert_story(Story(
                id=sid, canonical_url=f"https://ex.com/{sid}", canonical_title=title,
                canonical_summary="", relevance_score=0.5,
                published_at=datetime.fromisoformat(pub).replace(tzinfo=timezone.utc),
            ), conn=conn)
            storage.save_story_body(sid, f"Body of {title}. " * 30, conn=conn)
            storage.save_story_tags(sid, {
                "category": cat, "facts": facts, "magnitude": mag, "companies": comps,
                "geo": geo, "summary": f"Summary of {title}.",
            }, model="test", conn=conn)
        conn.commit()
        conn.close()
        p = mock.patch.object(config, "DB_PATH", db)
        p.start()
        self.addCleanup(p.stop)
        self.conn = qa._connect()
        self.addCleanup(self.conn.close)

    def ids(self, **kw) -> list[str]:
        return [s["id"] for s in qa.search_stories(self.conn, **kw)]


class SearchTest(_Archive):
    def test_biggest_ipos_this_month(self):
        got = self.ids(category="venture_ipo", published_after="2026-09-01")
        self.assertEqual(got, ["adarx", "eclat", "seed"])  # S by size, then B; August excluded

    def test_event_geo_and_magnitude_filters(self):
        self.assertEqual(self.ids(events=["ipo"]), ["adarx", "old"])
        self.assertEqual(self.ids(events=["ipo", "ipo_filing"]), ["adarx", "eclat", "old"])
        self.assertEqual(self.ids(geo="India", category="venture_ipo"), ["eclat", "seed"])
        self.assertEqual(self.ids(min_magnitude="A", category="venture_ipo"), ["adarx", "eclat", "old"])

    def test_non_healthcare_only_when_asked(self):
        self.assertNotIn("nse", self.ids(limit=25))
        self.assertIn("nse", self.ids(include_not_healthcare=True, limit=25))
        self.assertEqual(self.ids(category="not_healthcare"), ["nse"])

    def test_deal_size_bounds_convert_the_users_currency(self):
        cr200 = {"value": 200, "unit": "crore", "currency": "INR"}  # ~$22.7M
        self.assertEqual(self.ids(geo="India", max_amount=cr200), ["seed"])  # $300M Eclat out
        self.assertEqual(self.ids(min_amount=cr200), ["adarx", "eclat", "old"])  # no-amount FDA out
        self.assertEqual(self.ids(min_amount={"value": 1, "unit": "million", "currency": "USD"},
                                  max_amount={"value": 5, "unit": "million", "currency": "USD"}), ["seed"])
        # A bound it can't read fails the call, so the model retries rather
        # than presenting unfiltered deals as filtered.
        for bad in ({"value": "lots"}, {"value": 200, "unit": "cr", "currency": "INR"},
                    {"value": 5, "unit": "lakh", "currency": "INR"}):  # last is under $10k
            with self.assertRaises(ValueError):
                self.ids(max_amount=bad)
        out = json.loads(qa._dispatch(self.conn, "search_stories", {"max_amount": {"value": 200, "unit": "cr",
                                                                    "currency": "INR"}}, set()))
        self.assertIn("max_amount not understood", out["error"])

    def test_query_company_and_bad_values(self):
        self.assertEqual(self.ids(query="lirafugratinib"), ["fda"])
        self.assertEqual(self.ids(company="Eclat"), ["eclat"])
        # A wrong enum is ignored, not fatal.
        self.assertEqual(len(self.ids(category="crypto", geo="Mars", limit=25)), 5)

    def test_amount_shows_the_article_figure_with_usd(self):
        self.assertEqual(qa.money_display({"amount_usd": 5.4545e8, "amount_text": "\u20b94,800 Cr"}),
                         "\u20b94,800 Cr (~$545M)")
        self.assertEqual(qa.money_display({"amount_usd": 3.35e9, "amount_text": "$3,350M"}), "$3.35B")
        self.assertEqual(qa.money_display({"amount_usd": 4.463e8}), "~$446M")  # label from before the change
        self.assertIsNone(qa.money_display({"amount_usd": 350}))
        self.assertEqual(qa.money_display({"valuation_usd": 1.2e10, "valuation_text": "$12B"}, "valuation"), "$12B")

    def test_dates_are_parsed_and_bad_ones_ignored(self):
        self.assertNotIn("old", self.ids(published_after="2026/09/15", category="venture_ipo"))
        self.assertIn("eclat", self.ids(published_after="2026/09/15", category="venture_ipo"))
        self.assertIn("old", self.ids(published_after="15 September 2026", category="venture_ipo"))

    def test_get_story_never_hands_over_raw_money(self):
        conn = storage.connect(config.DB_PATH)
        _add(conn, "slip", "Slip IPO", "2026-09-20", "venture_ipo", {"event": "ipo", "amount_usd": 350}, ["Slip"], None)
        conn.close()
        s = qa.get_story(self.conn, "slip")
        self.assertEqual((s["amount_usd"], s["amount"], s["facts"]), (None, None, {"event": "ipo"}))

    def test_get_story_has_article_text(self):
        s = qa.get_story(self.conn, "fda")
        self.assertEqual((s["facts"]["product"], s["article_text"][:7]), ("lirafugratinib", "Body of"))
        self.assertIsNone(qa.get_story(self.conn, "nope"))


class AboutTest(_Archive):
    """`about` matches a topic by meaning: stories within the similarity floor,
    closest first by default; stories without an embedding never match."""
    TOPIC = [1, 0, 0, 0]

    def setUp(self) -> None:
        super().setUp()
        conn = storage.connect(config.DB_PATH)
        # cos to TOPIC: 1.0, 0.6, 0.0
        _add(conn, "on", "Lilly obesity pill pricing", "2026-09-20", "other_healthcare", {}, ["Eli Lilly"],
             [1, 0, 0, 0], mag="B")
        _add(conn, "near", "Novo cuts Wegovy price", "2026-09-25", "other_healthcare", {}, ["Novo Nordisk"],
             [0.6, 0, 0.8, 0], mag="S")
        _add(conn, "off", "Hospital ransomware attack", "2026-09-22", "other_healthcare", {}, ["Acme"],
             [0, 1, 0, 0], mag="S")
        conn.close()
        self.embed = lambda text: self.TOPIC

    def about(self, **kw) -> list[str]:
        return [s["id"] for s in qa.search_stories(self.conn, about="obesity drug pricing", embed=self.embed, **kw)]

    def test_closest_first_and_off_topic_dropped(self):
        self.assertEqual(self.about(), ["on", "near"])  # STORIES fixtures have no embedding: never match

    def test_explicit_sort_reorders_the_matches(self):
        self.assertEqual(self.about(sort="importance"), ["near", "on"])
        self.assertEqual(self.about(sort="recent"), ["near", "on"])

    def test_sort_never_trades_a_close_match_for_a_newer_loose_one(self):
        self.assertEqual(self.about(sort="recent", limit=1), ["on"])  # "near" is newer but looser

    def test_filters_still_apply(self):
        self.assertEqual(self.about(company="Lilly"), ["on"])

    def test_dispatch_passes_the_embedder(self):
        out = json.loads(qa._dispatch(self.conn, "search_stories", {"about": "obesity"}, set(), embed=self.embed))
        self.assertEqual([s["id"] for s in out["stories"]], ["on", "near"])
        out = json.loads(qa._dispatch(self.conn, "search_stories", {"about": "obesity"}, set()))
        self.assertIn("about needs an embedder", out["error"])


class MergeTest(_Archive):
    """One event told by several outlets merges; different news never does,
    even when it shares a company's first word, a company, or a label."""
    A, NEAR_A, A_ISH = [1, 0, 0, 0], [0.9, 0.3, 0, 0], [0.8, 0.6, 0, 0]  # cos to A: 0.95, 0.8

    def setUp(self) -> None:
        super().setUp()
        conn = storage.connect(config.DB_PATH)
        ipo = lambda usd: {"event": "ipo", "amount_usd": usd}
        _add(conn, "z1", "Zenyx IPO debut", "2026-09-25", "venture_ipo", ipo(5e8), ["Zenyx Pharmaceuticals"], self.A)
        _add(conn, "z2", "Zenyx upsized IPO", "2026-09-26", "venture_ipo", ipo(4e8), ["Zenyx"], self.NEAR_A)
        _add(conn, "zf", "Zenyx files S-1", "2026-09-18", "venture_ipo", {"event": "ipo_filing"}, ["Zenyx"], self.A_ISH)
        _add(conn, "zlate", "Zenyx follow-on", "2026-10-20", "venture_ipo", ipo(1e8), ["Zenyx"], self.NEAR_A)
        _add(conn, "ga", "General Atlantic takes stake", "2026-09-20", "pe_strategics",
             {"event": "stake"}, ["General Atlantic"], [0, 0, 1, 0])
        _add(conn, "gc", "General Catalyst takes stake", "2026-09-21", "pe_strategics",
             {"event": "stake"}, ["General Catalyst"], [0, 0, 0, 1])
        _add(conn, "l1", "Lilly drug approved", "2026-09-20", "fda_regulatory", {"event": "approval"}, ["Eli Lilly"], [0, 1, 0, 0])
        _add(conn, "l2", "Lilly drug recalled", "2026-09-21", "fda_regulatory", {"event": "recall"}, ["Eli Lilly"], [0, 0.95, 0.3, 0])
        conn.close()

    def test_one_event_from_several_outlets_merges_filing_included(self):
        got = qa.search_stories(self.conn, query="Zenyx")
        self.assertEqual([s["id"] for s in got], ["z1", "zlate"])  # zlate: 25 days later, its own event
        self.assertEqual(got[0]["more_links"], ["https://ex.com/z2", "https://ex.com/zf"])

    def test_different_news_never_merges(self):
        self.assertEqual(sorted(self.ids(query="General")), ["ga", "gc"])  # shared first word
        self.assertEqual(sorted(self.ids(query="Lilly")), ["l1", "l2"])    # same company, other event


def _call(name: str, args: dict, cid: str = "c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class _FakeOpenAI:
    """Round 1 asks for a search; round 2 answers with one real and one invented link."""
    def __init__(self, final: str):
        self.final, self.requests = final, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        first = len(self.requests) == 1
        msg = SimpleNamespace(
            content=None if first else self.final,
            tool_calls=[_call("search_stories", {"category": "venture_ipo", "events": ["ipo"]})] if first else None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100))


class AnswerTest(_Archive):
    def test_tool_loop_and_invented_links_are_unlinked(self):
        final = ("ADARx led the month. [MedCity](https://ex.com/adarx) and "
                 "[Made up](https://invented.example/x). See https://invented.example/y.")
        fake = _FakeOpenAI(final)
        text, usage = qa.answer("biggest IPOs this month?", client=fake)
        self.assertIn("[MedCity](https://ex.com/adarx)", text)
        self.assertNotIn("invented.example", text)
        self.assertIn("Made up", text)  # the link text survives, unlinked
        self.assertEqual((usage["rounds"], usage["tool_calls"]), (2, 1))
        # The tool result went back to the model, and the system prompt carries coverage.
        tool_msg = fake.requests[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual([s["id"] for s in json.loads(tool_msg["content"])["stories"]], ["adarx", "old"])
        self.assertIn("labelled stories", fake.requests[0]["messages"][0]["content"])
        self.assertEqual(usage["searches"], [{"tool": "search_stories", "category": "venture_ipo", "events": ["ipo"]}])
        # Round 1 is forced to search; later rounds may answer.
        self.assertEqual([r["tool_choice"] for r in fake.requests], ["required", "auto"])
        self.assertIn("archive_covers_from", json.loads(tool_msg["content"]))

    def test_follow_up_may_reuse_links_from_the_thread(self):
        fake = _FakeOpenAI("Eclat again: [Bloomberg](https://bb.example/eclat).")
        history = [{"role": "assistant", "content": "Eclat, see <https://bb.example/eclat|Bloomberg>."}]
        text, _ = qa.answer("and in India?", history, client=fake)
        self.assertIn("[Bloomberg](https://bb.example/eclat)", text)

    def test_invented_links_in_every_form_are_unlinked(self):
        text = ("A <https://bad.example/1|Fake> B <https://bad.example/2> C (https://bad.example/3) "
                "D <https://ex.com/adarx|Real> E [ok](https://ex.com/adarx).")
        out = qa.keep_known_links(text, {"https://ex.com/adarx"})
        self.assertNotIn("bad.example", out)
        self.assertIn("Fake", out)
        self.assertIn("<https://ex.com/adarx|Real>", out)
        self.assertIn("[ok](https://ex.com/adarx)", out)

    def test_urls_people_post_in_the_thread_are_not_citable(self):
        fake = _FakeOpenAI("See [this](https://evil.example/x).")
        history = [{"role": "user", "content": "look at https://evil.example/x"}]
        text, _ = qa.answer("and?", history, client=fake)
        self.assertNotIn("evil.example", text)

    def test_usd_rounds_up_into_the_next_unit(self):
        self.assertEqual((qa._usd_short(999.6e6), qa._usd_short(999.6e3), qa._usd_short(3.35e9)),
                         ("$1B", "$1M", "$3.35B"))

    def test_em_dashes_are_replaced(self):
        self.assertEqual(qa._house_style("archive\u2014the earliest"), "archive, the earliest")

    def test_closing_offer_is_dropped(self):
        self.assertEqual(qa._house_style("ADARx led.\n\nLet me know if you want more."), "ADARx led.")
        self.assertEqual(qa._house_style("Let me know is a phrase.\nADARx led."), "Let me know is a phrase.\nADARx led.")
        self.assertEqual(qa._house_style("None above $50M. If you want all rounds, let me know."), "None above $50M.")
        self.assertEqual(qa._house_style("- **Disha raised \u20b944 Cr.** Would you like more?"),
                         "- **Disha raised \u20b944 Cr.**")

    def test_bad_tool_arguments_cost_one_call_not_the_answer(self):
        out = qa._dispatch(self.conn, "search_stories", {"limit": "lots"}, set())
        self.assertIn("error", json.loads(out))


class BotTest(unittest.TestCase):
    def test_markdown_to_slack(self):
        self.assertEqual(bot.to_mrkdwn("**ADARx** [MedCity](https://x.y/z)"), "*ADARx* <https://x.y/z|MedCity>")
        self.assertEqual(bot.to_mrkdwn("- one\n* two"), "• one\n• two")

    def test_chunking(self):
        self.assertEqual(bot.chunk("short"), ["short"])
        parts = bot.chunk("line\n" * 2000, limit=100)
        self.assertTrue(len(parts) > 1 and all(len(p) <= 100 for p in parts))

    def test_dedupe(self):
        seen = bot._Seen()
        self.assertTrue(seen.first("e1"))
        self.assertFalse(seen.first("e1"))

    def _client(self):
        c = mock.Mock()
        c.chat_postMessage.return_value = {"ts": "999.1"}
        c.conversations_replies.return_value = {"messages": [
            {"user": "U1", "text": "<@UBOT> biggest IPOs?"},
            {"user": "UBOT", "bot_id": "B1", "text": "ADARx led."},
            {"user": "U2", "bot_id": "B2", "text": "another bot"},
            {"user": "U1", "text": "<@UBOT> and in India?"},
        ]}
        return c

    def test_follow_up_gets_thread_history_and_answer_replaces_placeholder(self):
        client, got = self._client(), {}
        def fake_answer(q, history):
            got.update(q=q, history=history)
            return "**Eclat** in India.", {"rounds": 1}
        with mock.patch.object(bot, "_log"):
            bot.handle(client, "UBOT", {"channel": "C1", "ts": "2.0", "thread_ts": "1.0",
                                        "user": "U1", "text": "<@UBOT> and in India?"}, answer=fake_answer)
        self.assertEqual(got["q"], "and in India?")
        self.assertEqual(got["history"], [{"role": "user", "content": "biggest IPOs?"},
                                          {"role": "assistant", "content": "ADARx led."}])
        client.chat_update.assert_called_once_with(channel="C1", ts="999.1", text="*Eclat* in India.")

    def test_openai_billing_failure_is_explained(self):
        import httpx, openai
        err = openai.RateLimitError("x", response=httpx.Response(429, request=httpx.Request("POST", "https://a.b")),
                                    body={"code": "insufficient_quota"})
        client = self._client()
        def boom(q, history):
            raise err
        with mock.patch.object(bot, "_log") as log:
            bot.handle(client, "UBOT", {"channel": "C1", "ts": "1.0", "user": "U1", "text": "hi <@UBOT>"}, answer=boom)
        self.assertIn("out of credits", client.chat_update.call_args.kwargs["text"])
        self.assertIn("RateLimitError", log.call_args.args[0]["error"])

    def test_empty_mention_gets_help(self):
        client = self._client()
        bot.handle(client, "UBOT", {"channel": "C1", "ts": "1.0", "text": "<@UBOT>"}, answer=None)
        self.assertEqual(client.chat_postMessage.call_args.kwargs["text"], bot.HELP)

    def test_rejected_token_exits_clean_instead_of_restart_looping(self):
        with mock.patch.object(config, "SLACK_APP_TOKEN", "xapp-test"), \
             mock.patch.object(config, "SLACK_BOT_TOKEN", "xoxb-test"), \
             mock.patch("slack_bolt.App", side_effect=RuntimeError("The server responded with: invalid_auth")):
            self.assertEqual(bot.main(), 0)

    def test_unconfigured_bot_exits_clean(self):
        with mock.patch.object(config, "SLACK_APP_TOKEN", ""):
            self.assertEqual(bot.main(), 0)


if __name__ == "__main__":
    unittest.main()
