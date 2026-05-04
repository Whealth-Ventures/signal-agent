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
        self.assertEqual(config.PERPLEXITY_MODEL_RANK, "sonar-reasoning")
        self.assertEqual(config.PERPLEXITY_RECENCY, "day")

    def test_budget_constants(self) -> None:
        self.assertEqual(config.MAX_PERPLEXITY_CALLS_PER_DAY, 60)
        self.assertEqual(config.DEDUPE_LOOKBACK_DAYS, 7)
        self.assertEqual(config.DIGEST_TOP_N, 5)

    def test_embedding_model(self) -> None:
        self.assertEqual(config.EMBEDDING_MODEL, "text-embedding-3-small")

    def test_schedule(self) -> None:
        self.assertEqual(config.DIGEST_TZ, "Asia/Kolkata")
        self.assertEqual(config.DIGEST_HOUR_LOCAL, 10)


class ConfigEnvTest(unittest.TestCase):
    def test_recipients_parsed_as_tuple(self) -> None:
        self.assertIsInstance(config.DIGEST_RECIPIENTS, tuple)
        for addr in config.DIGEST_RECIPIENTS:
            self.assertIn("@", addr)
            self.assertEqual(addr, addr.strip())

    def test_check_env_passes_with_real_dotenv(self) -> None:
        # The .env in the repo is the source of truth for this run; if it's
        # missing keys, this should fail and tell us which.
        config.check_env()


if __name__ == "__main__":
    unittest.main()
