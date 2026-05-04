"""Smoke tests for src/query_planner.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import query_planner as qp  # noqa: E402


class KeywordsTest(unittest.TestCase):
    def test_count_matches_inspection(self) -> None:
        # India 982 + US 1080 + Cross-Cutting 217 = 2279 (per inspection step)
        self.assertEqual(len(qp.load_keywords()), 2279)

    def test_geographies(self) -> None:
        geos = {r.geography for r in qp.load_keywords()}
        self.assertSetEqual(geos, {"India", "US", "Global"})

    def test_no_blank_keyword(self) -> None:
        for r in qp.load_keywords():
            self.assertTrue(r.keyword)


class VoicesTest(unittest.TestCase):
    def test_count(self) -> None:
        # India 108 + US 118 = 226
        self.assertGreaterEqual(len(qp.load_voices()), 220)

    def test_both_geographies(self) -> None:
        geos = {v.geography for v in qp.load_voices()}
        self.assertSetEqual(geos, {"India", "US"})

    def test_voices_have_names(self) -> None:
        for v in qp.load_voices():
            self.assertTrue(v.name)


class NewslettersTest(unittest.TestCase):
    def test_count(self) -> None:
        self.assertGreaterEqual(len(qp.load_newsletters()), 55)

    def test_banner_row_skipped(self) -> None:
        # Row 0 is a banner with text "Newsletters, Substacks, Podcasts and Publications".
        # Row 1 is the real header with "#" / "Tier" / "Publication / Channel".
        # First data row should be a real publication name (e.g., "KFF Health News").
        first = qp.load_newsletters()[0]
        self.assertNotIn("Newsletters,", first.name)
        self.assertNotIn("Publication", first.name)
        self.assertTrue(first.name)


class CompanyPagesTest(unittest.TestCase):
    def test_count(self) -> None:
        self.assertGreaterEqual(len(qp.load_company_pages()), 55)

    def test_banner_row_skipped(self) -> None:
        first = qp.load_company_pages()[0]
        self.assertNotIn("Notable Healthcare", first.name)
        self.assertTrue(first.name)


class QueryPlansTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plans = qp.build_query_plans()

    def test_plan_count_30_to_40(self) -> None:
        self.assertGreaterEqual(len(self.plans), 30)
        self.assertLessEqual(len(self.plans), 40)

    def test_no_keyword_dropped(self) -> None:
        total_in_plans = sum(p.keyword_count_total for p in self.plans)
        self.assertEqual(total_in_plans, len(qp.load_keywords()))

    def test_unique_ids(self) -> None:
        ids = [p.id for p in self.plans]
        self.assertEqual(len(ids), len(set(ids)))

    def test_geography_set(self) -> None:
        geos = {p.geography for p in self.plans}
        self.assertSetEqual(geos, {"India", "US", "Global"})

    def test_geography_ordering(self) -> None:
        geos = [p.geography for p in self.plans]
        # India block, then US block, then Global block — no interleaving.
        i = geos.index("India") if "India" in geos else 0
        u = geos.index("US") if "US" in geos else 0
        g = geos.index("Global") if "Global" in geos else 0
        self.assertLess(i, u)
        self.assertLess(u, g)

    def test_every_plan_has_substance(self) -> None:
        geo_to_label_substring = {
            "India": "India",
            "US": "United States",
            "Global": "globally",
        }
        for p in self.plans:
            with self.subTest(p=p.id):
                self.assertTrue(p.prompt_text)
                self.assertIn(geo_to_label_substring[p.geography], p.prompt_text)
                if p.voice_names:
                    # Voice-anchored plan: no buckets/keywords; first voice name in prompt.
                    self.assertEqual(p.sub_buckets, ())
                    self.assertEqual(p.keyword_sample, ())
                    self.assertEqual(p.keyword_count_total, 0)
                    self.assertIn(p.voice_names[0], p.prompt_text)
                else:
                    self.assertGreater(len(p.sub_buckets), 0)
                    self.assertGreater(len(p.keyword_sample), 0)
                    self.assertIn(p.bucket, p.prompt_text)

    def test_voice_plans_present(self) -> None:
        ids = {p.id for p in self.plans}
        self.assertIn("india__tier1_voices", ids)
        self.assertIn("us__tier1_voices", ids)

    def test_voice_plans_have_named_voices(self) -> None:
        by_id = {p.id: p for p in self.plans}
        india_voices = by_id["india__tier1_voices"].voice_names
        us_voices = by_id["us__tier1_voices"].voice_names
        # Per inspection: 12 India tier-1, 18 US tier-1.
        self.assertEqual(len(india_voices), 12)
        self.assertEqual(len(us_voices), 18)
        for n in india_voices + us_voices:
            self.assertTrue(n.strip())

    def test_determinism(self) -> None:
        # Build twice; ids and prompt_texts should be byte-identical.
        a = qp.build_query_plans()
        b = qp.build_query_plans()
        self.assertEqual([p.id for p in a], [p.id for p in b])
        self.assertEqual([p.prompt_text for p in a], [p.prompt_text for p in b])


if __name__ == "__main__":
    unittest.main()
