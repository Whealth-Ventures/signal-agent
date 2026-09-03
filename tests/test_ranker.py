"""Smoke tests for src/ranker.py — magnitude rubric, per-category selection,
top-5 summary, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import ranker  # noqa: E402
import storage  # noqa: E402
from models import Story, story_id  # noqa: E402
from perplexity_client import ChatResponse  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Fixed timestamp so recency (the new primary within-tier sort, post-#5) ties
# across fixtures and the score-based selection assertions still hold. Pass
# `published_at` explicitly to test recency ordering.
_FIXED_TS = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _mk_story(
    slug: str,
    *,
    score: float = 0.5,
    summary: str = "",
    priority_bucket: str | None = None,
    geo: str | None = None,
    published_at: datetime | None = None,
) -> Story:
    url = f"https://e.example/{slug}"
    return Story(
        id=story_id(url),
        canonical_url=url,
        # Healthcare-worded so the ranker's topicality gate (is_healthcare, now
        # on the main path per #5) keeps these fixtures as candidates.
        canonical_title=f"Hospital deal {slug}",
        canonical_summary=summary or f"Healthcare funding news about {slug}.",
        published_at=published_at or _FIXED_TS,
        relevance_score=score,
        priority_bucket=priority_bucket,
        geo=geo,
    )


class FakePerplexityClient:
    def __init__(self, response_text: str, *, cost: float = 0.005,
                 model: str = "sonar-reasoning") -> None:
        self.response_text = response_text
        self.cost = cost
        self.model = model
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, model: str = "", recency: str | None = None,
                 query_id: str = "", system: str | None = None,
                 timeout: float | None = None) -> ChatResponse:
        self.calls.append({
            "prompt": prompt, "model": model, "query_id": query_id,
            "system": system, "timeout": timeout,
        })
        return ChatResponse(
            text=self.response_text, citations=(),
            model=model or self.model,
            prompt_tokens=100, completion_tokens=50,
            estimated_cost_usd=self.cost, raw={},
        )


# --- Prompt build ------------------------------------------------------

class BuildPromptTest(unittest.TestCase):
    def test_groups_by_priority_bucket(self) -> None:
        stories = [
            _mk_story("a", score=0.8, priority_bucket="venture_ipo"),
            _mk_story("b", score=0.6, priority_bucket="fda_regulatory"),
            _mk_story("c", score=0.4, priority_bucket=None),
        ]
        grouped = ranker._group_for_prompt(stories)
        prompt = ranker.build_prompt(grouped)
        self.assertIn("Venture & IPO", prompt)
        self.assertIn("FDA & Regulatory", prompt)
        self.assertIn("Other", prompt)
        for s in stories:
            self.assertIn(s.id, prompt)

    def test_includes_magnitude_rubric(self) -> None:
        grouped = ranker._group_for_prompt([_mk_story("a")])
        prompt = ranker.build_prompt(grouped)
        self.assertIn("TIER S", prompt)
        self.assertIn("TIER C", prompt)

    def test_truncates_long_summary(self) -> None:
        long = "x" * 1000
        grouped = ranker._group_for_prompt([_mk_story("a", summary=long)])
        prompt = ranker.build_prompt(grouped)
        # Summary is truncated to RANKER_SUMMARY_MAX_CHARS (350) before the prompt.
        self.assertLess(prompt.count("x"), config.RANKER_SUMMARY_MAX_CHARS + 10)


# --- JSON extraction ---------------------------------------------------

class ExtractJsonTest(unittest.TestCase):
    def test_clean_json(self) -> None:
        obj = ranker._extract_json('{"stories": []}')
        self.assertEqual(obj, {"stories": []})

    def test_fenced_json(self) -> None:
        obj = ranker._extract_json('```json\n{"stories": [{"x": 1}]}\n```')
        self.assertEqual(obj, {"stories": [{"x": 1}]})

    def test_with_preamble(self) -> None:
        obj = ranker._extract_json('Sure!\n{"stories": []}')
        self.assertEqual(obj, {"stories": []})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json("nope"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(ranker._extract_json(""))


# --- Tier coercion + parse ranked --------------------------------------

class CoerceTierTest(unittest.TestCase):
    def test_valid_tiers(self) -> None:
        for t in ("S", "A", "B", "C", "s", "a"):
            self.assertEqual(ranker._coerce_tier(t), t.upper())

    def test_invalid_tier(self) -> None:
        self.assertIsNone(ranker._coerce_tier("D"))
        self.assertIsNone(ranker._coerce_tier(None))
        self.assertIsNone(ranker._coerce_tier(""))


class ParseRankedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stories = [_mk_story(s, score=0.5 + i * 0.05)
                        for i, s in enumerate(("a", "b", "c"))]
        self.by_id = {s.id: s for s in self.stories}

    def test_well_formed_json(self) -> None:
        a, b, c = self.stories
        text = json.dumps({"stories": [
            {"story_id": b.id, "tier": "S", "one_liner": "Big news"},
            {"story_id": a.id, "tier": "A", "one_liner": "Decent news"},
            {"story_id": c.id, "tier": "C", "one_liner": "Drop me"},
        ]})
        decisions, buckets, _geos, fallback = ranker.parse_ranked(text, self.by_id)
        self.assertFalse(fallback)
        self.assertEqual(decisions[b.id], ("S", "Big news"))
        self.assertEqual(decisions[a.id], ("A", "Decent news"))
        self.assertEqual(decisions[c.id], ("C", "Drop me"))

    def test_garbage_marks_fallback(self) -> None:
        decisions, buckets, _geos, fallback = ranker.parse_ranked("not json", self.by_id)
        self.assertTrue(fallback)
        self.assertEqual(decisions, {})
        self.assertEqual(buckets, {})

    def test_unknown_id_dropped(self) -> None:
        text = json.dumps({"stories": [
            {"story_id": "definitely_not_an_id", "tier": "S", "one_liner": "x"},
        ]})
        decisions, _buckets, _geos, _ = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(decisions, {})

    def test_invalid_tier_dropped(self) -> None:
        text = json.dumps({"stories": [
            {"story_id": self.stories[0].id, "tier": "D", "one_liner": "x"},
        ]})
        decisions, _buckets, _geos, _ = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(decisions, {})

    def test_bucket_parsed_when_valid(self) -> None:
        a = self.stories[0]
        text = json.dumps({"stories": [
            {"story_id": a.id, "tier": "S", "one_liner": "x",
             "bucket": "fda_regulatory"},
        ]})
        decisions, buckets, _geos, _ = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(buckets[a.id], "fda_regulatory")

    def test_invalid_bucket_ignored(self) -> None:
        a = self.stories[0]
        text = json.dumps({"stories": [
            {"story_id": a.id, "tier": "S", "one_liner": "x",
             "bucket": "not_a_real_bucket"},
        ]})
        decisions, buckets, _geos, _ = ranker.parse_ranked(text, self.by_id)
        self.assertIn(a.id, decisions)
        self.assertNotIn(a.id, buckets)

    def test_geo_parsed_and_normalised(self) -> None:
        a, b, c = self.stories
        text = json.dumps({"stories": [
            {"story_id": a.id, "tier": "S", "one_liner": "x", "geo": "US"},
            {"story_id": b.id, "tier": "A", "one_liner": "x", "geo": "india"},
            {"story_id": c.id, "tier": "A", "one_liner": "x", "geo": "GLOBAL"},
        ]})
        _d, _b, geos, _f = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(geos[a.id], "US")
        self.assertEqual(geos[b.id], "India")
        self.assertEqual(geos[c.id], "Global")

    def test_invalid_or_missing_geo_ignored(self) -> None:
        a, b, c = self.stories
        text = json.dumps({"stories": [
            {"story_id": a.id, "tier": "S", "one_liner": "x", "geo": "APAC"},
            {"story_id": b.id, "tier": "A", "one_liner": "x", "geo": 42},
            {"story_id": c.id, "tier": "A", "one_liner": "x"},
        ]})
        decisions, _b, geos, _f = ranker.parse_ranked(text, self.by_id)
        self.assertEqual(geos, {})
        # Unusable geo must not cost the story its tier or one-liner.
        self.assertEqual(len(decisions), 3)


class EffectiveGeoTest(unittest.TestCase):
    """The ranker read the story; the inherited geo only knows where we found
    it. Real 3 Sept 2026 mislabels: Digital Health News (an Indian outlet) on
    Sanford/North Memorial in Minnesota, and medicaldialogues.in on Novartis
    halting CAR-T trials."""

    def test_llm_geo_overrides_inherited_publication_geo(self) -> None:
        st = _mk_story("sanford", geo="India")   # inherited from an Indian outlet
        self.assertEqual(ranker._effective_geo(st, {st.id: "US"}), "US")

    def test_llm_geo_overrides_inherited_plan_geo(self) -> None:
        st = _mk_story("indian_funding", geo="Global")  # found by a Global plan
        self.assertEqual(ranker._effective_geo(st, {st.id: "India"}), "India")

    def test_falls_back_to_inherited_when_llm_silent(self) -> None:
        st = _mk_story("x", geo="India")
        self.assertEqual(ranker._effective_geo(st, {}), "India")

    def test_unknown_stays_unknown(self) -> None:
        st = _mk_story("x", geo=None)
        self.assertIsNone(ranker._effective_geo(st, {}))


# --- Selection ---------------------------------------------------------

class SelectionTest(unittest.TestCase):
    """Uniform per-bucket selection: fixed top summary + up to per_bucket_max
    per bucket, no 'Other', top stories not duplicated into their bucket."""

    def test_per_bucket_cap(self) -> None:
        # 3 stories in one bucket, no top pulled → body capped at per_bucket_max.
        stories = [_mk_story(f"s{i}", priority_bucket="venture_ipo")
                   for i in range(3)]
        grouped = ranker._group_for_prompt(stories)
        decisions = {s.id: ("A", "x") for s in stories}
        top, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=0,
        )
        self.assertEqual(top, [])
        self.assertEqual(len(by_priority["venture_ipo"]), 2)

    def test_drop_tier_c(self) -> None:
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo"),
            _mk_story("s2", priority_bucket="venture_ipo"),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("S", "x"), stories[1].id: ("C", "drop")}
        _, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=0,
        )
        ids = {r.story.id for r in by_priority.get("venture_ipo", [])}
        self.assertIn(stories[0].id, ids)
        self.assertNotIn(stories[1].id, ids)

    def test_top_not_duplicated_in_bucket(self) -> None:
        # The top-summary pick is pulled OUT of its bucket body (no dup).
        stories = [
            _mk_story("s1", priority_bucket="venture_ipo", score=0.9),
            _mk_story("s2", priority_bucket="venture_ipo", score=0.8),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("S", "x"), stories[1].id: ("A", "y")}
        top, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=1,
        )
        self.assertEqual(len(top), 1)
        top_ids = {r.story.id for r in top}
        body_ids = {r.story.id for r in by_priority.get("venture_ipo", [])}
        self.assertTrue(top_ids.isdisjoint(body_ids))

    def test_bucket_empty_when_sole_story_promoted(self) -> None:
        # A bucket whose only story is promoted to the top shows nothing in the
        # body (we never duplicate; can't manufacture a second story).
        stories = [_mk_story("s1", priority_bucket="venture_ipo")]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("S", "x")}
        top, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=1,
        )
        self.assertEqual(len(top), 1)
        self.assertNotIn("venture_ipo", by_priority)

    def test_unbucketed_stories_ignored(self) -> None:
        # _select only ever surfaces the 8 priority buckets — a story with no
        # bucket (OTHER_KEY) produces nothing. (rank_stories force-buckets
        # upstream so this doesn't happen in practice.)
        stories = [_mk_story("s1", priority_bucket=None)]
        grouped = ranker._group_for_prompt(stories)
        decisions = {stories[0].id: ("S", "x")}
        top, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=5,
        )
        self.assertEqual(top, [])
        self.assertEqual(by_priority, {})

    def test_distinct_buckets_each_capped(self) -> None:
        stories = [
            _mk_story("v1", priority_bucket="venture_ipo"),
            _mk_story("v2", priority_bucket="venture_ipo"),
            _mk_story("v3", priority_bucket="venture_ipo"),
            _mk_story("a1", priority_bucket="ai_healthcare"),
        ]
        grouped = ranker._group_for_prompt(stories)
        decisions = {s.id: ("A", "x") for s in stories}
        _, by_priority = ranker._select(
            grouped, decisions, per_bucket_max=2, top_summary_size=0,
        )
        self.assertEqual(len(by_priority["venture_ipo"]), 2)
        self.assertEqual(len(by_priority["ai_healthcare"]), 1)


class TopSummaryTest(unittest.TestCase):
    def test_picks_n_highest_magnitude(self) -> None:
        rs = lambda slug, tier, score: ranker.RankedStory(
            story=_mk_story(slug, score=score, priority_bucket="venture_ipo"),
            tier=tier, one_liner="x",
        )
        by_priority = {"venture_ipo": [
            rs("a", "S", 0.5),
            rs("b", "A", 0.9),
            rs("c", "B", 0.95),
        ]}
        top = ranker._top_summary(by_priority, [], n=2)
        # S beats A and B regardless of score
        self.assertEqual([r.tier for r in top], ["S", "A"])

    def test_excludes_other(self) -> None:
        rs = lambda slug, tier, pri: ranker.RankedStory(
            story=_mk_story(slug, score=0.5, priority_bucket=pri),
            tier=tier, one_liner="x",
        )
        by_priority = {"venture_ipo": [rs("a", "A", "venture_ipo")]}
        other = [rs("b", "S", None)]
        top = ranker._top_summary(by_priority, other, n=5)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].story.id, _mk_story("a").id)

    def test_score_beats_recency_within_a_tier(self) -> None:
        """Regression, 2 Sept 2026: recency used to be the primary key, so a
        newer low-scored item displaced the day's best story."""
        older_better = _mk_story(
            "ultrahuman", score=0.68, priority_bucket="venture_ipo",
            published_at=_FIXED_TS - timedelta(hours=6),
        )
        newer_worse = _mk_story(
            "oped", score=0.31, priority_bucket="venture_ipo",
            published_at=_FIXED_TS,
        )
        by_priority = {"venture_ipo": [
            ranker.RankedStory(story=newer_worse, tier="A", one_liner="x"),
            ranker.RankedStory(story=older_better, tier="A", one_liner="x"),
        ]}
        top = ranker._top_summary(by_priority, [], n=1)
        self.assertEqual(top[0].story.id, older_better.id)


class CollapseDuplicatesTest(unittest.TestCase):
    """Real pairs that each ate two slots in the 3 Sept 2026 India digest."""

    @staticmethod
    def _story(sid_url: str, title: str, score: float) -> Story:
        return Story(
            id=story_id(sid_url),
            canonical_url=sid_url,
            canonical_title=title,
            canonical_summary="",
            published_at=_FIXED_TS,
            relevance_score=score,
        )

    def test_same_publisher_two_url_paths_collapses(self) -> None:
        # BioSpectrum serves one article under two path prefixes.
        a = self._story(
            "https://www.biospectrumindia.com/news/16/28410/aig-hospitals-invest",
            "AIG Hospitals to invest Rs 2000 Cr in new ecosystem", 0.59,
        )
        b = self._story(
            "https://www.biospectrumindia.com/news/101/28410/aig-hospitals-invest",
            "AIG Hospitals to invest Rs 2000 Cr in new ecosystem", 0.59,
        )
        kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 1)

    def test_different_hosts_same_title_collapses_keeping_best_score(self) -> None:
        weak = self._story(
            "https://www.digitalhealthnews.com/luma-fertility-30-centres",
            "Luma Fertility plans to expand to 30 centres nationally", 0.52,
        )
        strong = self._story(
            "https://www.biospectrumindia.com/news/20/28407/luma-fertility",
            "Luma Fertility plans to expand to 30 centres nationally", 0.71,
        )
        kept, dropped = ranker.collapse_duplicates([weak, strong])
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0].id, strong.id)

    def test_title_punctuation_and_case_ignored(self) -> None:
        a = self._story("https://a.example/x-story-here", "Gland Pharma clears USFDA", 0.4)
        b = self._story("https://b.example/y-story-there", "gland pharma  clears usfda!", 0.3)
        _kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 1)

    def test_distinct_stories_are_untouched_and_order_preserved(self) -> None:
        a = self._story("https://a.example/alpha-article-one", "Alpha raises money", 0.3)
        b = self._story("https://b.example/beta-article-two", "Beta buys Gamma", 0.9)
        c = self._story("https://c.example/gamma-article-three", "Delta wins approval", 0.6)
        kept, dropped = ranker.collapse_duplicates([a, b, c])
        self.assertEqual(dropped, 0)
        self.assertEqual([s.id for s in kept], [a.id, b.id, c.id])

    def test_query_string_articles_stay_distinct(self) -> None:
        """Regression: some publishers carry the article id ONLY in the query.
        Discarding it collapsed three unrelated pharmabiz.com articles into
        one, silently dropping two from the candidate pool."""
        a = self._story(
            "https://pharmabiz.com/NewsDetails.aspx?aid=101",
            "Cipla launches new inhaler", 0.7,
        )
        b = self._story(
            "https://pharmabiz.com/NewsDetails.aspx?aid=202",
            "Sun Pharma gets USFDA nod", 0.6,
        )
        c = self._story(
            "https://pharmabiz.com/NewsDetails.aspx?aid=303",
            "Lupin recalls batch", 0.5,
        )
        kept, dropped = ranker.collapse_duplicates([a, b, c])
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 3)

    def test_same_query_article_still_collapses(self) -> None:
        a = self._story(
            "https://pharmabiz.com/NewsDetails.aspx?aid=101",
            "Cipla launches new inhaler", 0.7,
        )
        b = self._story(
            "https://pharmabiz.com/NewsDetails.aspx?aid=101&utm_source=x",
            "Cipla launches inhaler (syndicated)", 0.4,
        )
        kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0].id, a.id)

    def test_tracking_params_do_not_defeat_the_url_key(self) -> None:
        a = self._story("https://a.example/article-one", "T one", 0.5)
        b = self._story(
            "https://a.example/article-one?utm_campaign=z&cid=abc", "T two", 0.4,
        )
        _kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 1)

    def test_script_name_last_segment_does_not_collide(self) -> None:
        """`NewsDetails.aspx` is 16 chars, so the length guard alone passed it.
        A real slug has a hyphen; a script name doesn't."""
        a = self._story(
            "https://x.example/NewsDetails.aspx?aid=1", "Story one", 0.5,
        )
        b = self._story(
            "https://x.example/NewsDetails.aspx?aid=2", "Story two", 0.4,
        )
        _kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 0)

    def test_short_last_segments_do_not_collide(self) -> None:
        """/feed and /news must not make two articles look like one."""
        a = self._story("https://a.example/health/feed", "First article", 0.5)
        b = self._story("https://a.example/business/feed", "Second article", 0.4)
        _kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 0)

    def test_trailing_slash_and_www_normalised(self) -> None:
        a = self._story("https://www.a.example/some-long-article/", "Title one", 0.5)
        b = self._story("https://a.example/some-long-article", "Title two", 0.4)
        _kept, dropped = ranker.collapse_duplicates([a, b])
        self.assertEqual(dropped, 1)


class DefaultBucketTest(unittest.TestCase):
    """The catch-all must be a deliberate choice, not sheet row order — an
    unbucketed opinion piece filed under 'Venture & IPO' on 2 Sept 2026."""

    def test_configured_bucket_wins(self) -> None:
        target = config.PRIORITY_BUCKETS[-1].key
        self.assertNotEqual(target, config.PRIORITY_BUCKETS[0].key)
        with mock.patch.object(config, "DEFAULT_BUCKET", target):
            self.assertEqual(ranker._default_bucket_key(), target)

    def test_unset_falls_back_to_first_bucket(self) -> None:
        with mock.patch.object(config, "DEFAULT_BUCKET", ""):
            self.assertEqual(
                ranker._default_bucket_key(), config.PRIORITY_BUCKETS[0].key,
            )

    def test_unknown_key_falls_back_rather_than_crashing(self) -> None:
        with mock.patch.object(config, "DEFAULT_BUCKET", "not_a_bucket"):
            self.assertEqual(
                ranker._default_bucket_key(), config.PRIORITY_BUCKETS[0].key,
            )

    def test_unbucketed_story_lands_in_the_configured_bucket(self) -> None:
        target = config.PRIORITY_BUCKETS[-1].key
        story = _mk_story("oped", score=0.4, priority_bucket=None)
        with mock.patch.object(config, "DEFAULT_BUCKET", target):
            eff = ranker._effective_bucket(
                story, {}, ranker._valid_bucket_keys(),
                ranker._default_bucket_key(),
            )
        self.assertEqual(eff, target)


class FilterByGeoTest(unittest.TestCase):
    """Selection must be re-run, not filtered: `_select` strips a promoted
    story from its bucket body, so dropping a highlight used to leave a hole
    nothing could fill. On the 3 Sept data that cut the highlights to one."""

    @staticmethod
    def _rs(slug: str, *, geo: str, bucket: str, score: float, tier="A"):
        return ranker.RankedStory(
            story=_mk_story(slug, score=score, geo=geo, priority_bucket=bucket),
            tier=tier, one_liner=f"line {slug}",
        )

    def test_dropped_highlight_slot_is_refilled(self) -> None:
        us1 = self._rs("us1", geo="US", bucket="venture_ipo", score=0.9)
        us2 = self._rs("us2", geo="US", bucket="venture_ipo", score=0.85)
        ind = self._rs("ind", geo="India", bucket="venture_ipo", score=0.4)
        # As _select would leave it: the two US stories promoted, India in body.
        r = ranker.RankingResult(
            top_summary=[us1, us2], by_priority={"venture_ipo": [ind]},
            other=[], candidates_count=3, used_fallback=False,
            cost_usd=0.0, elapsed_seconds=0.0, flat=(us1, us2, ind),
        )
        out = ranker.filter_by_geo(r, {"India", "Global"})
        # The India story is promoted into the vacated highlight slot rather
        # than the highlights going empty.
        self.assertEqual([x.story.id for x in out.top_summary], [ind.story.id])
        self.assertEqual(out.by_priority, {})
        self.assertEqual([x.story.id for x in out.flat], [ind.story.id])

    def test_unknown_geo_still_kept_by_both_channels(self) -> None:
        unk = self._rs("unk", geo=None, bucket="venture_ipo", score=0.5)
        r = ranker.RankingResult(
            top_summary=[unk], by_priority={}, other=[], candidates_count=1,
            used_fallback=False, cost_usd=0.0, elapsed_seconds=0.0, flat=(unk,),
        )
        for allowed in ({"India", "Global"}, {"US", "Global"}):
            out = ranker.filter_by_geo(r, allowed)
            self.assertEqual(len(out.flat), 1)

    def test_no_duplication_between_top_and_bodies(self) -> None:
        items = [
            self._rs(f"s{i}", geo="India", bucket="venture_ipo", score=0.9 - i / 100)
            for i in range(6)
        ]
        r = ranker.RankingResult(
            top_summary=items[:2], by_priority={"venture_ipo": items[2:]},
            other=[], candidates_count=6, used_fallback=False,
            cost_usd=0.0, elapsed_seconds=0.0, flat=tuple(items),
        )
        out = ranker.filter_by_geo(r, {"India", "Global"})
        top_ids = {x.story.id for x in out.top_summary}
        body_ids = {x.story.id for v in out.by_priority.values() for x in v}
        self.assertEqual(top_ids & body_ids, set())
        ids = [x.story.id for x in out.flat]
        self.assertEqual(len(ids), len(set(ids)))


class OrderWithinCategoryTest(unittest.TestCase):
    def test_higher_score_wins_the_bucket_slot(self) -> None:
        """The Ultrahuman case: per_bucket_max is small, so ordering decides
        whether the day's best story ships at all."""
        older_better = _mk_story(
            "ultrahuman", score=0.68,
            published_at=_FIXED_TS - timedelta(hours=6),
        )
        newer_worse = _mk_story(
            "smartwalker", score=0.31, published_at=_FIXED_TS,
        )
        ordered = ranker._ordered_within_category(
            [newer_worse, older_better], decisions={},
        )
        self.assertEqual(
            [s.id for s, _t, _ol in ordered],
            [older_better.id, newer_worse.id],
        )

    def test_tier_still_outranks_score(self) -> None:
        low_score_top_tier = _mk_story("s_tier", score=0.20)
        high_score_low_tier = _mk_story("b_tier", score=0.95)
        decisions = {
            low_score_top_tier.id: ("S", "x"),
            high_score_low_tier.id: ("B", "x"),
        }
        ordered = ranker._ordered_within_category(
            [high_score_low_tier, low_score_top_tier], decisions=decisions,
        )
        self.assertEqual(
            [s.id for s, _t, _ol in ordered],
            [low_score_top_tier.id, high_score_low_tier.id],
        )

    def test_recency_breaks_a_score_tie(self) -> None:
        newer = _mk_story("newer", score=0.50, published_at=_FIXED_TS)
        older = _mk_story(
            "older", score=0.50, published_at=_FIXED_TS - timedelta(hours=3),
        )
        ordered = ranker._ordered_within_category([older, newer], decisions={})
        self.assertEqual(
            [s.id for s, _t, _ol in ordered], [newer.id, older.id],
        )


class RemovePromotedTest(unittest.TestCase):
    def test_drops_promoted_and_removes_empty_categories(self) -> None:
        rs = lambda slug: ranker.RankedStory(
            story=_mk_story(slug, score=0.5), tier="S", one_liner="x",
        )
        a, b = rs("a"), rs("b")
        by_priority = {"venture_ipo": [a], "fda_regulatory": [b]}
        # Both promoted → both categories empty → both hidden
        after = ranker._remove_promoted(by_priority, [a, b])
        self.assertEqual(after, {})


# --- Orchestrator ------------------------------------------------------

class _OrchestratorBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = storage.connect(self.db_path)
        storage.init_db(conn=self.conn)
        self._patch_logs = mock.patch.object(
            config, "LOGS_DIR", Path(self.tmp.name),
        )
        self._patch_logs.start()
        # Pin the ranking vendor to Perplexity so the injected fake client is
        # the one actually used. Without this, a machine with ANTHROPIC_API_KEY
        # in its .env would route these tests to the real Claude API.
        self._patch_key = mock.patch.object(config, "ANTHROPIC_API_KEY", "")
        self._patch_key.start()
        self.conn.commit()

    def tearDown(self) -> None:
        self._patch_key.stop()
        self._patch_logs.stop()
        self.conn.close()
        self.tmp.cleanup()


class EmptyPoolTest(_OrchestratorBase):
    def test_empty_returns_empty(self) -> None:
        client = FakePerplexityClient('{"stories":[]}')
        result = ranker.rank_stories(conn=self.conn, client=client)
        self.assertEqual(result.top_summary, [])
        self.assertEqual(result.by_priority, {})
        self.assertEqual(result.other, [])
        self.assertEqual(client.calls, [])  # no LLM call


class FullPathTest(_OrchestratorBase):
    def _seed(self) -> list[Story]:
        stories = [
            _mk_story("a", score=0.85, priority_bucket="fda_regulatory", geo="US"),
            _mk_story("b", score=0.78, priority_bucket="pe_strategics", geo="US"),
            _mk_story("c", score=0.74, priority_bucket="hospital_ma", geo="India"),
            _mk_story("d", score=0.71, priority_bucket="venture_ipo", geo="US"),
            _mk_story("e", score=0.68, priority_bucket="venture_ipo", geo="India"),
            _mk_story("f", score=0.55, priority_bucket="ai_healthcare", geo="Global"),
            _mk_story("g", score=0.30, priority_bucket=None, geo=None),
        ]
        for s in stories:
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        return stories

    def test_llm_geo_reaches_the_result_and_the_channel_filter(self) -> None:
        """End-to-end: the ranker's geo must beat the inherited one, and must
        then drive channel routing. Story 'c' is inherited India (an Indian
        publication) but the ranker says the news is US — it must not ship to
        the India channel."""
        stories = self._seed()
        c = stories[2]
        text = json.dumps({"stories": [
            {"story_id": s.id, "tier": "A", "one_liner": f"line {i}",
             **({"geo": "US"} if s.id == c.id else {})}
            for i, s in enumerate(stories[:6])
        ]})
        result = ranker.rank_stories(
            conn=self.conn, client=FakePerplexityClient(text),
        )
        by_id = {r.story.id: r for r in result.flat}
        self.assertEqual(by_id[c.id].story.geo, "US")

        india = ranker.filter_by_geo(result, {"India", "Global"})
        self.assertNotIn(c.id, {r.story.id for r in india.flat})
        us = ranker.filter_by_geo(result, {"US", "Global"})
        self.assertIn(c.id, {r.story.id for r in us.flat})

    def test_inherited_geo_kept_when_llm_omits_it(self) -> None:
        stories = self._seed()
        e = stories[4]   # inherited India
        text = json.dumps({"stories": [
            {"story_id": s.id, "tier": "A", "one_liner": "x"}
            for s in stories[:6]
        ]})
        result = ranker.rank_stories(
            conn=self.conn, client=FakePerplexityClient(text),
        )
        by_id = {r.story.id: r for r in result.flat}
        self.assertEqual(by_id[e.id].story.geo, "India")

    def test_thin_coverage_counts_as_a_degraded_run(self) -> None:
        """A response deciding 1 of 7 stories is the shape max-token
        truncation takes (FEEDBACK #11). It used to read as healthy, so no
        Slack notice fired and the undecided remainder silently fell back to
        the inherited geo and bucket."""
        stories = self._seed()
        text = json.dumps({"stories": [
            {"story_id": stories[0].id, "tier": "S", "one_liner": "only one",
             "bucket": "fda_regulatory", "geo": "US"},
        ]})
        result = ranker.rank_stories(
            conn=self.conn, client=FakePerplexityClient(text),
        )
        self.assertTrue(result.used_fallback)

    def test_full_coverage_is_not_flagged(self) -> None:
        stories = self._seed()
        text = json.dumps({"stories": [
            {"story_id": s.id, "tier": "A", "one_liner": "x"} for s in stories
        ]})
        result = ranker.rank_stories(
            conn=self.conn, client=FakePerplexityClient(text),
        )
        self.assertFalse(result.used_fallback)

    def test_blocked_domain_already_in_the_pool_is_dropped(self) -> None:
        """The save-time filter is forward-only, so adding a host must take
        effect on the next run rather than in 30 days."""
        self._seed()
        bad = Story(
            id=story_id("https://www.facebook.com/x/posts/ai-daily-reporter"),
            canonical_url="https://www.facebook.com/x/posts/ai-daily-reporter",
            canonical_title="Hospital deal roundup from a Facebook repost",
            canonical_summary="Healthcare funding news.",
            published_at=_FIXED_TS,
            relevance_score=0.99,          # would otherwise top the digest
            geo="India",
        )
        storage.upsert_story(bad, conn=self.conn)
        self.conn.commit()
        text = json.dumps({"stories": [
            {"story_id": bad.id, "tier": "S", "one_liner": "x"},
        ]})
        with mock.patch.object(config, "BLOCKED_DOMAINS", ("facebook.com",)):
            result = ranker.rank_stories(
                conn=self.conn, client=FakePerplexityClient(text),
            )
        self.assertNotIn(bad.id, {r.story.id for r in result.flat})

    def test_llm_response_used(self) -> None:
        stories = self._seed()
        decisions = {
            stories[0].id: ("S", "FDA approves something"),
            stories[1].id: ("S", "KKR acquires"),
            stories[2].id: ("S", "Apollo deal"),
            stories[3].id: ("S", "Hims raises"),
            stories[4].id: ("A", "Sarvodaya files DRHP"),
            stories[5].id: ("B", "AI seed"),
            stories[6].id: ("C", "Drop me"),
        }
        text = json.dumps({"stories": [
            {"story_id": sid, "tier": tier, "one_liner": ol}
            for sid, (tier, ol) in decisions.items()
        ]})
        client = FakePerplexityClient(text)
        result = ranker.rank_stories(conn=self.conn, client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["model"], config.PERPLEXITY_MODEL_RANK)
        self.assertEqual(len(result.top_summary), 5)
        # All 4 S items should be in top 5; the 5th is the highest A.
        top_tiers = [r.tier for r in result.top_summary]
        self.assertEqual(top_tiers.count("S"), 4)
        self.assertEqual(top_tiers.count("A"), 1)
        # The Tier-C story is dropped.
        all_ids = {r.story.id for r in result.flat}
        self.assertNotIn(stories[6].id, all_ids)

    def test_garbage_response_falls_back_to_score_order(self) -> None:
        self._seed()
        client = FakePerplexityClient("no JSON here")
        result = ranker.rank_stories(conn=self.conn, client=client)
        self.assertTrue(result.used_fallback)
        # Fallback treats everything as Tier A and applies normal selection.
        self.assertGreater(len(result.flat), 0)


class ExcludeRecentlySentTest(_OrchestratorBase):
    def test_sent_stories_excluded_from_candidates(self) -> None:
        """High-scoring stories already shipped in a recent digest must not
        reach the ranker — otherwise evergreens win the candidate pool forever."""
        now = _utcnow()
        old_hi = _mk_story("old_hi", score=0.95, priority_bucket="venture_ipo")
        new_hi = _mk_story("new_hi", score=0.60, priority_bucket="venture_ipo")
        for s in (old_hi, new_hi):
            storage.upsert_story(s, conn=self.conn)
        did = storage.create_digest("2026-05-20", ["x"], conn=self.conn)
        storage.add_story_to_digest(did, old_hi.id, rank=1, conn=self.conn)
        storage.mark_digest_sent(did, now - timedelta(days=5), conn=self.conn)
        self.conn.commit()

        client = FakePerplexityClient('{"stories":[]}')
        result = ranker.rank_stories(conn=self.conn, client=client)

        self.assertEqual(result.candidates_count, 1)
        all_ids = {r.story.id for r in result.flat}
        self.assertNotIn(old_hi.id, all_ids)


class LoggingTest(_OrchestratorBase):
    def test_log_record_written(self) -> None:
        for s in (_mk_story("a", score=0.7, priority_bucket="venture_ipo"),
                  _mk_story("b", score=0.6, priority_bucket="fda_regulatory")):
            storage.upsert_story(s, conn=self.conn)
        self.conn.commit()
        client = FakePerplexityClient('{"stories":[]}')
        ranker.rank_stories(conn=self.conn, client=client)
        log_files = list(config.LOGS_DIR.glob("ranker_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        # File has 2+ records — read the LAST line (the summary)
        lines = log_files[0].read_text().strip().splitlines()
        rec = json.loads(lines[-1])
        self.assertEqual(rec["candidates_count"], 2)


class RankStoriesVendorTest(_OrchestratorBase):
    """The regression guard, at the level the bug actually lived: rank_stories().

    Testing _build_ranker_client() alone does NOT catch it — that function was
    always correct. The bug was its call site skipping it whenever a client was
    passed, which is exactly what main.py does on every production run.
    """

    def test_passed_client_unused_when_anthropic_configured(self) -> None:
        storage.upsert_story(
            _mk_story("a", score=0.7, priority_bucket="venture_ipo"), conn=self.conn,
        )
        self.conn.commit()

        passed = FakePerplexityClient('{"stories":[]}')      # main.py's fetch client
        claude = FakePerplexityClient('{"stories":[]}', model="claude-sonnet-4-5")

        with mock.patch.object(config, "RANKER_PROVIDER", "anthropic"), \
             mock.patch.object(config, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             mock.patch("anthropic_client.AnthropicClient", return_value=claude):
            ranker.rank_stories(conn=self.conn, client=passed)

        self.assertEqual(passed.calls, [], "ranking must not go to the fetch client")
        self.assertEqual(len(claude.calls), 1, "ranking must go to Claude")
        self.assertEqual(claude.calls[0]["model"], config.ANTHROPIC_MODEL_RANK)


class RankerVendorSelectionTest(unittest.TestCase):
    """The ranking vendor must follow config, not whoever passed a client.

    Regression: main.py hands rank_stories() its Perplexity *fetch* client for
    budget accounting. rank_stories() used to consult _build_ranker_client()
    only when no client was passed, so that argument silently pinned every
    production run to Perplexity — the Claude path never executed, despite
    ranker_provider=anthropic and a key being set.
    """

    def test_passed_client_is_reused_on_the_perplexity_path(self) -> None:
        # Reusing it matters: a client built here carries no geo scope and
        # would bill against the wrong per-(date, geo) budget log.
        passed = FakePerplexityClient('{"stories":[]}')
        with mock.patch.object(config, "RANKER_PROVIDER", "perplexity"), \
             mock.patch.object(config, "ANTHROPIC_API_KEY", ""):
            client, model = ranker._build_ranker_client(fallback=passed)
        self.assertIs(client, passed)
        self.assertEqual(model, config.PERPLEXITY_MODEL_RANK)

    def test_no_key_falls_back_even_when_provider_is_anthropic(self) -> None:
        passed = FakePerplexityClient('{"stories":[]}')
        with mock.patch.object(config, "RANKER_PROVIDER", "anthropic"), \
             mock.patch.object(config, "ANTHROPIC_API_KEY", ""):
            client, model = ranker._build_ranker_client(fallback=passed)
        self.assertIs(client, passed)
        self.assertEqual(model, config.PERPLEXITY_MODEL_RANK)


if __name__ == "__main__":
    unittest.main()
