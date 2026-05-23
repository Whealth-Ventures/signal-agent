"""Single source of truth for paths, env, and tunable constants.

Import is cheap: only loads .env, resolves paths, and ensures data/ subdirs exist.
No HTTP, no DB, no logging setup. Call check_env() from main.py at startup to
fail fast if required env vars are missing.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


# --- Paths ---------------------------------------------------------------

INPUTS_DIR = ROOT / "inputs"
KEYWORDS_XLSX = INPUTS_DIR / "keywords.xlsx"
VOICES_XLSX = INPUTS_DIR / "voices.xlsx"

CONTENT_DIR = ROOT / "content"

DATA_DIR = ROOT / "data"
DB_DIR = DATA_DIR / "db"
DB_PATH = DB_DIR / "agent.db"
VECTOR_STORE_DIR = DATA_DIR / "vector_store"
LOGS_DIR = DATA_DIR / "logs"

for _d in (DB_DIR, VECTOR_STORE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --- Env ----------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_int(name: str) -> int:
    v = _env(name)
    return int(v) if v else 0


OPENAI_API_KEY = _env("OPENAI_API_KEY")
PERPLEXITY_API_KEY = _env("PERPLEXITY_API_KEY")

SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL")
# Human-readable channel label, e.g. "#healthcare-signal". Optional — used only
# in the digests.recipients audit column and in logs; the webhook URL already
# pins which channel actually receives the post.
SLACK_CHANNEL_LABEL = _env("SLACK_CHANNEL_LABEL") or "(slack)"


# --- Constants ----------------------------------------------------------

# Budget / dedupe
MAX_PERPLEXITY_CALLS_PER_DAY = 60
DEDUPE_LOOKBACK_DAYS = 7
DIGEST_TOP_N = 10
DAILY_BUDGET_USD = 3.0

# Perplexity
PERPLEXITY_MODEL_FETCH = "sonar-pro"
PERPLEXITY_MODEL_RANK = "sonar-reasoning-pro"
PERPLEXITY_RECENCY = "day"

# Embeddings
EMBEDDING_MODEL = "text-embedding-3-small"

# HTTP
HTTP_TIMEOUT_S = 30
# sonar-reasoning-pro does extended chain-of-thought; 30s is too tight when the
# candidate prompt is large (timed out 4× in a row on --max-plans 20).
HTTP_TIMEOUT_RANK_S = 120
HTTP_MAX_RETRIES = 4
URL_VALIDATION_TIMEOUT_S = 10

# Schedule (digest is sent at 10am IST)
DIGEST_TZ = "Asia/Kolkata"
DIGEST_HOUR_LOCAL = 10


# --- Validation ---------------------------------------------------------

REQUIRED_ENV = (
    "OPENAI_API_KEY",
    "PERPLEXITY_API_KEY",
    "SLACK_WEBHOOK_URL",
)


def check_env() -> None:
    """Raise RuntimeError listing every missing required env var. Call at startup."""
    missing = [k for k in REQUIRED_ENV if not _env(k)]
    if missing:
        raise RuntimeError(
            "Missing required env vars in .env: " + ", ".join(missing)
        )

    if not KEYWORDS_XLSX.exists():
        raise RuntimeError(f"Keywords file not found: {KEYWORDS_XLSX}")
    if not VOICES_XLSX.exists():
        raise RuntimeError(f"Voices file not found: {VOICES_XLSX}")
    if not CONTENT_DIR.is_dir():
        raise RuntimeError(f"Content dir not found: {CONTENT_DIR}")
