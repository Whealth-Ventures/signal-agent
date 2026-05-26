"""Smoke tests for src/query_planner.py — new single-tab schema + Track A/B."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import query_planner as qp  # noqa: E402


class KeywordsTest(unittest.TestCase):
    def test_count_reasonable(self) -> None:
        # Master Keywords tab — single tab with ~2,240 keywords.
        self.assertGreater(len(qp.load_keywords()), 2000)

    def test_geographies(self) -> None:
        geos = {r.geography for r in qp.load_keywords()}
        self.assertSetEqual(geos, {"India", "US", "Both"})

    def test_no_blank_keyword(self) -> None:
        for r in qp.load_keywords():
            self.assertTrue(r.keyword)


class VoicesTest(unittest.TestCase):
    def test_count(self) -> None:
        # India ~170 + US ~170.
        self.assertGreater(len(qp.load_voices()), 300)

    def test_both_geographies(self) -> None:
        geos = {v.geography for v in qp.load_voices()}
        self.assertSetEqual(geos, {"India", "US"})

    def test_voices_have_names(self) -> None:
        for v in qp.load_voices():
            self.assertTrue(v.name)

    def test_tier1_marked(self) -> None:
        # Per the user-curated marks in voices.xlsx column I.
        india_t1 = [v for v in qp.load_voices() if v.tier == 1 and v.geography == "India"]
        us_t1 = [v for v in qp.load_voices() if v.tier == 1 and v.geography == "US"]
        self.assertGreaterEqual(len(india_t1), 20)
        self.assertGreaterEqual(len(us_t1), 20)


class NewslettersTest(unittest.TestCase):
    def test_count(self) -> None:
        self.assertGreaterEqual(len(qp.load_newsletters()), 20)

    def test_banner_row_skipped(self) -> None:
        first = qp.load_newsletters()[0]
        self.assertNotIn("Newsletters,", first.name)
        self.assertTrue(first.name)


class CompanyPagesTest(unittest.TestCase):
    def test_count(self) -> None:
        self.assertGreaterEqual(len(qp.load_company_pages()), 50)

    def test_banner_row_skipped(self) -> None:
        first = qp.load_company_pages()[0]
        self.assertNotIn("Notable Healthcare", first.name)
        self.assertTrue(first.name)


class FirmAdditionsTest(unittest.TestCase):
    def test_count(self) -> None:
        # ~44 firms across A/B/C categories.
        self.assertGreaterEqual(len(qp.load_firm_additions()), 40)

    def test_firms_have_names(self) -> None:
        for f in qp.load_firm_additions():
            self.assertTrue(f.firm)

    def test_categories_present(self) -> None:
        cats = {f.category for f in qp.load_firm_additions()}
        # User has three category prefixes (A/B/C).
        self.assertGreaterEqual(len(cats), 3)


class QueryPlansTest(unittest.TestCase):
    TEST_DATE = date(2026, 5, 26)

    @classmethod
    def setUpClass(cls) -> None:
        cls.plans = qp.build_query_plans(today=cls.TEST_DATE)

    def test_unique_ids(self) -> None:
        ids = [p.id for p in self.plans]
        self.assertEqual(len(ids), len(set(ids)))

    def test_determinism(self) -> None:
        a = qp.build_query_plans(today=self.TEST_DATE)
        b = qp.build_query_plans(today=self.TEST_DATE)
        self.assertEqual([p.id for p in a], [p.id for p in b])
        self.assertEqual([p.prompt_text for p in a], [p.prompt_text for p in b])

    def test_track_a_count_is_13(self) -> None:
        track_a = [p for p in self.plans if p.track == "A"]
        # 8 priority buckets, but AI emits 2 plans → 13 total
        # (venture_ipo: 2, pe_strategics: 2, hospital_ma: 2, mso_rollups: 1,
        #  fda_regulatory: 2, hot_tas: 1, us_medicare: 1, ai_healthcare: 2).
        self.assertEqual(len(track_a), 13)

    def test_track_b_matches_config(self) -> None:
        track_b = [p for p in self.plans if p.track == "B"]
        self.assertEqual(len(track_b), config.TRACK_B_PLANS_PER_DAY)

    def test_voice_plans_present(self) -> None:
        ids = {p.id for p in self.plans}
        self.assertIn("voice__india_t1", ids)
        self.assertIn("voice__us_t1", ids)

    def test_firm_plan_present(self) -> None:
        ids = {p.id for p in self.plans}
        self.assertIn("firm__india_pe_vc", ids)


class TrackACoverageTest(unittest.TestCase):
    """Every PriorityBucket × geo combination should produce exactly one plan,
    except AI in Healthcare which produces two (ventures + clinical)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plans = [
            p for p in qp.build_query_plans(today=date(2026, 5, 26))
            if p.track == "A"
        ]

    def test_each_priority_bucket_represented(self) -> None:
        seen_keys = {p.priority_bucket for p in self.plans}
        expected = {b.key for b in config.PRIORITY_BUCKETS}
        self.assertSetEqual(seen_keys, expected)

    def test_ai_has_two_variants(self) -> None:
        ai_plans = [p for p in self.plans if p.priority_bucket == "ai_healthcare"]
        self.assertEqual(len(ai_plans), 2)

    def test_priority_bucket_set_on_track_a(self) -> None:
        for p in self.plans:
            self.assertIsNotNone(p.priority_bucket, f"{p.id} missing priority_bucket")


class TrackBRotationTest(unittest.TestCase):
    def test_same_date_same_plans(self) -> None:
        d = date(2026, 5, 26)
        a = {p.id for p in qp.build_query_plans(today=d) if p.track == "B"}
        b = {p.id for p in qp.build_query_plans(today=d) if p.track == "B"}
        self.assertSetEqual(a, b)

    def test_consecutive_days_dont_overlap(self) -> None:
        d1 = date(2026, 5, 26)
        d2 = date(2026, 5, 27)
        a = {p.id for p in qp.build_query_plans(today=d1) if p.track == "B"}
        b = {p.id for p in qp.build_query_plans(today=d2) if p.track == "B"}
        self.assertEqual(len(a & b), 0, "Track B windows shouldn't overlap day-to-day")

    def test_full_coverage_in_14_days(self) -> None:
        """Across 14 days, every non-priority (sub_bucket, geo) should appear."""
        start = date(2026, 5, 26)
        seen = set()
        for offset in range(config.TRACK_B_ROTATION_DAYS):
            d = date.fromordinal(start.toordinal() + offset)
            for p in qp.build_query_plans(today=d):
                if p.track == "B":
                    seen.add(p.id)
        all_subs = qp._all_non_priority_subs(qp.load_keywords())
        self.assertEqual(len(seen), len(all_subs))

    def test_track_b_priority_bucket_is_none(self) -> None:
        for p in qp.build_query_plans(today=date(2026, 5, 26)):
            if p.track == "B":
                self.assertIsNone(p.priority_bucket)


class GeoFilteringTest(unittest.TestCase):
    """Verify Track A geo-filter logic: India plan only includes India + Both
    keywords; US plan only includes US + Both; Global plan includes everything."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kws = qp.load_keywords()

    def test_india_plan_excludes_us_only_keywords(self) -> None:
        venture_india = next(
            p for p in qp.build_query_plans(today=date(2026, 5, 26))
            if p.id == "pri__venture_ipo__india"
        )
        # Every keyword in this plan should be from a row marked India or Both.
        eligible = {
            kr.keyword for kr in self.kws
            if kr.sub_bucket in venture_india.sub_buckets
            and kr.geography in ("India", "Both")
        }
        for kw in venture_india.keyword_sample:
            self.assertIn(kw, eligible)

    def test_us_plan_excludes_india_only_keywords(self) -> None:
        venture_us = next(
            p for p in qp.build_query_plans(today=date(2026, 5, 26))
            if p.id == "pri__venture_ipo__us"
        )
        eligible = {
            kr.keyword for kr in self.kws
            if kr.sub_bucket in venture_us.sub_buckets
            and kr.geography in ("US", "Both")
        }
        for kw in venture_us.keyword_sample:
            self.assertIn(kw, eligible)


class VoiceAndFirmPlansTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plans_by_id = {p.id: p for p in qp.build_query_plans(today=date(2026, 5, 26))}

    def test_india_voice_plan_names_tier1(self) -> None:
        plan = self.plans_by_id["voice__india_t1"]
        self.assertGreater(len(plan.voice_names), 0)
        self.assertIn(plan.voice_names[0], plan.prompt_text)

    def test_us_voice_plan_names_tier1(self) -> None:
        plan = self.plans_by_id["voice__us_t1"]
        self.assertGreater(len(plan.voice_names), 0)
        self.assertIn(plan.voice_names[0], plan.prompt_text)

    def test_firm_plan_names_first_firm(self) -> None:
        plan = self.plans_by_id["firm__india_pe_vc"]
        self.assertGreater(len(plan.firm_names), 0)
        self.assertIn(plan.firm_names[0], plan.prompt_text)


if __name__ == "__main__":
    unittest.main()
