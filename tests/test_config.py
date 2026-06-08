"""Smoke tests for src/config.py — paths resolve, data dirs are created, env loader works."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402


class ConfigPathsTest(unittest.TestCase):
    def test_root_is_repo_root(self) -> None:
        self.assertEqual(config.ROOT, ROOT)

    def test_input_files_exist(self) -> None:
        self.assertTrue(config.KEYWORDS_XLSX.is_file())
        self.assertTrue(config.VOICES_XLSX.is_file())

    def test_content_dir_exists(self) -> None:
        self.assertTrue(config.CONTENT_DIR.is_dir())

    def test_data_dirs_created_on_import(self) -> None:
        self.assertTrue(config.DB_DIR.is_dir())
        self.assertTrue(config.VECTOR_STORE_DIR.is_dir())
        self.assertTrue(config.LOGS_DIR.is_dir())

    def test_db_path_under_db_dir(self) -> None:
        self.assertEqual(config.DB_PATH.parent, config.DB_DIR)
        self.assertEqual(config.DB_PATH.name, "agent.db")


class ConfigConstantsTest(unittest.TestCase):
    def test_perplexity_models(self) -> None:
        self.assertEqual(config.PERPLEXITY_MODEL_FETCH, "sonar-pro")
        self.assertEqual(config.PERPLEXITY_MODEL_RANK, "sonar-reasoning-pro")
        self.assertEqual(config.PERPLEXITY_RECENCY, "day")

    def test_budget_constants(self) -> None:
        self.assertEqual(config.MAX_PERPLEXITY_CALLS_PER_DAY, 60)
        self.assertEqual(config.DEDUP_WINDOW_DAYS, 30)
        self.assertEqual(config.MAX_DIGEST_ITEMS, 22)
        self.assertEqual(config.TOP_SUMMARY_SIZE, 5)

    def test_track_b_constants(self) -> None:
        # track_b_plans_per_day is now a budget-safety CAP; the actual plans/day
        # is derived from the rotation length (7-day full cycle).
        self.assertEqual(config.TRACK_B_PLANS_PER_DAY, 40)
        self.assertEqual(config.TRACK_B_ROTATION_DAYS, 7)

    def test_embedding_model(self) -> None:
        self.assertEqual(config.EMBEDDING_MODEL, "text-embedding-3-small")

    def test_schedule(self) -> None:
        self.assertEqual(config.DIGEST_TZ, "Asia/Kolkata")
        self.assertEqual(config.DIGEST_HOUR_LOCAL, 8)

    def test_prompts_load_from_disk(self) -> None:
        # Both prompts come from prompts/*.md, not inline strings.
        self.assertTrue(config.RANKER_SYSTEM_PROMPT)
        self.assertIn("VC firm", config.RANKER_SYSTEM_PROMPT)
        self.assertTrue(config.MAGNITUDE_RUBRIC)
        self.assertIn("TIER S", config.MAGNITUDE_RUBRIC)
        self.assertIn("TIER C", config.MAGNITUDE_RUBRIC)

    def test_boosters_dict_has_expected_keys(self) -> None:
        # Locks in the booster table shape so future edits to the dict are
        # deliberate, not accidental.
        expected = {
            "tier1_voice", "trusted_publication", "firm_mention",
            "funding", "m_and_a", "regulatory", "product",
            "leadership", "listicle", "opinion",
        }
        self.assertEqual(set(config.BOOSTERS), expected)


class PriorityBucketsTest(unittest.TestCase):
    def test_has_eight_buckets(self) -> None:
        self.assertEqual(len(config.PRIORITY_BUCKETS), 8)

    def test_keys_are_unique_and_kebab_friendly(self) -> None:
        keys = [b.key for b in config.PRIORITY_BUCKETS]
        self.assertEqual(len(keys), len(set(keys)))
        for k in keys:
            self.assertRegex(k, r"^[a-z][a-z0-9_]*$")

    def test_every_bucket_has_at_least_one_sub_bucket(self) -> None:
        for b in config.PRIORITY_BUCKETS:
            self.assertTrue(b.sub_buckets, f"{b.key} has no sub_buckets")

    def test_geos_are_valid(self) -> None:
        allowed = {"India", "US", "Global"}
        for b in config.PRIORITY_BUCKETS:
            self.assertTrue(b.geos, f"{b.key} has no geos")
            for g in b.geos:
                self.assertIn(g, allowed)


class SourceTier1Test(unittest.TestCase):
    def test_nonempty(self) -> None:
        self.assertGreater(len(config.SOURCE_TIER_1), 10)

    def test_contains_expected_us_outlets(self) -> None:
        for host in ("bloomberg.com", "wsj.com", "statnews.com"):
            self.assertIn(host, config.SOURCE_TIER_1)

    def test_contains_expected_india_outlets(self) -> None:
        for host in ("livemint.com", "economictimes.indiatimes.com"):
            self.assertIn(host, config.SOURCE_TIER_1)


class MagnitudeRubricTest(unittest.TestCase):
    def test_mentions_all_four_tiers(self) -> None:
        rubric = config.MAGNITUDE_RUBRIC
        for tier in ("TIER S", "TIER A", "TIER B", "TIER C"):
            self.assertIn(tier, rubric, f"rubric missing {tier}")


class ConfigEnvTest(unittest.TestCase):
    def test_slack_webhook_url_loaded(self) -> None:
        self.assertIsInstance(config.SLACK_WEBHOOK_URL, str)
        # Real .env should have a Slack webhook URL configured.
        self.assertTrue(
            config.SLACK_WEBHOOK_URL.startswith("https://hooks.slack.com/"),
            f"SLACK_WEBHOOK_URL doesn't look like a Slack hook: "
            f"{config.SLACK_WEBHOOK_URL!r}",
        )

    def test_channel_label_has_default(self) -> None:
        # Optional; falls back to "(slack)" so the digest record always has
        # something to display.
        self.assertIsInstance(config.SLACK_CHANNEL_LABEL, str)
        self.assertTrue(config.SLACK_CHANNEL_LABEL)

    def test_check_env_passes_with_real_dotenv(self) -> None:
        # The .env in the repo is the source of truth for this run; if it's
        # missing keys, this should fail and tell us which.
        config.check_env()


if __name__ == "__main__":
    unittest.main()
