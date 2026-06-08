"""Single source of truth for paths, env, prompt loading, and re-exported
tunables.

Import is cheap: loads .env, resolves paths, ensures data/ subdirs exist, and
reads inputs/tuning.xlsx via `tunables`. No HTTP, no DB, no logging setup.
Call check_env() from main.py at startup to fail fast on missing env vars.

Tunable numbers, regex patterns, priority-bucket structure, and the source-
tier list all live in `inputs/tuning.xlsx`. This module just re-exposes them
as module-level constants so the rest of the codebase keeps reading
`config.MAX_PERPLEXITY_CALLS_PER_DAY` etc. unchanged. See docs/EDITING.md.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from tunables import PriorityBucket, load_tunables

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


# --- Paths ---------------------------------------------------------------

INPUTS_DIR = ROOT / "inputs"
KEYWORDS_XLSX = INPUTS_DIR / "keywords.xlsx"
VOICES_XLSX = INPUTS_DIR / "voices.xlsx"
TUNING_XLSX = INPUTS_DIR / "tuning.xlsx"

CONTENT_DIR = INPUTS_DIR / "content"
PROMPTS_DIR = ROOT / "prompts"

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
# Anthropic powers the single ranking/tiering/one-liner call (see ranker.py).
# Optional: when unset, the ranker falls back to Perplexity sonar-reasoning-pro.
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL")
# Bot token + channel ID power the chat.postMessage path. When SLACK_BOT_TOKEN
# is set we post via Web API (returns a message ts that lets us join Slack
# reactions to the digest in the feedback loop). When unset we fall back to
# SLACK_WEBHOOK_URL — same message body, but no ts capture.
SLACK_BOT_TOKEN = _env("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = _env("SLACK_CHANNEL_ID")
SLACK_CHANNEL_LABEL = _env("SLACK_CHANNEL_LABEL") or "(slack)"

# Vercel Blob read/write token — used by the feedback puller to fetch
# Slack-event blobs written by the admin app's receiver.
BLOB_READ_WRITE_TOKEN = _env("BLOB_READ_WRITE_TOKEN")


# --- Tunables (loaded from inputs/tuning.xlsx) --------------------------
#
# The Excel file is the source of truth for every value below. To edit any of
# them: open inputs/tuning.xlsx (Excel for the web or desktop), find the row,
# change the value, save. The next pipeline run picks up the change — no code
# edits required. See docs/EDITING.md for the lever map.

_t = load_tunables()

# Budget
MAX_PERPLEXITY_CALLS_PER_DAY = _t.get_int("max_perplexity_calls_per_day")
DAILY_BUDGET_USD = _t.get_float("daily_budget_usd")

# Digest shape
MAX_DIGEST_ITEMS = _t.get_int("max_digest_items")
TOP_SUMMARY_SIZE = _t.get_int("top_summary_size")
# Max stories shown per priority bucket in the digest body (1-2 each). Keeps the
# digest uniform day to day; the top-summary is separate and not bucketed.
PER_BUCKET_MAX = _t.get_int("per_bucket_max", 2)
# Floor: if normal S/A selection lands below this, the ranker backfills with the
# best remaining Tier-B stories so slow news days don't produce a thin digest.
# 0 disables the floor (pure threshold behaviour). Default 18 if the xlsx row
# hasn't been added yet.
TARGET_DIGEST_MIN = _t.get_int("target_digest_min", 18)

# Dedup
DEDUP_WINDOW_DAYS = _t.get_int("dedup_window_days")
HISTORICAL_DEDUP_THRESHOLD = _t.get_float("historical_dedup_threshold")

# Scoring
CLUSTER_SIMILARITY_THRESHOLD = _t.get_float("cluster_similarity_threshold")
TOP_K_CONTENT_SIMILARITY = _t.get_int("top_k_content_similarity")
SUMMARY_TRUNCATE_FOR_EMBED = _t.get_int("summary_truncate_for_embed")

# Boosters
BOOSTERS = _t.boosters

# Ranker
MIN_CANDIDATE_SCORE = _t.get_float("min_candidate_score")
ONE_LINER_MAX_CHARS = _t.get_int("one_liner_max_chars")
RANKER_SUMMARY_MAX_CHARS = _t.get_int("ranker_summary_max_chars")

# Perplexity
PERPLEXITY_MODEL_FETCH = _t.get_str("perplexity_model_fetch")
PERPLEXITY_MODEL_RANK = _t.get_str("perplexity_model_rank")
PERPLEXITY_RECENCY = _t.get_str("perplexity_recency")

# Ranker vendor selection. "anthropic" routes the single ranking call to Claude;
# "perplexity" keeps it on sonar-reasoning-pro. Either way, an unset
# ANTHROPIC_API_KEY forces the Perplexity fallback (see ranker.py).
RANKER_PROVIDER = _t.get_str("ranker_provider")
ANTHROPIC_MODEL_RANK = _t.get_str("anthropic_model_rank")
ANTHROPIC_MAX_TOKENS_RANK = _t.get_int("anthropic_max_tokens_rank", 4096)

# Embeddings
EMBEDDING_MODEL = _t.get_str("embedding_model")

# HTTP
HTTP_TIMEOUT_S = _t.get_int("http_timeout_s")
HTTP_TIMEOUT_RANK_S = _t.get_int("http_timeout_rank_s")
HTTP_MAX_RETRIES = _t.get_int("http_max_retries")
URL_VALIDATION_TIMEOUT_S = _t.get_int("url_validation_timeout_s")

# Schedule (digest sent at 10am IST)
DIGEST_TZ = _t.get_str("digest_tz")
DIGEST_HOUR_LOCAL = _t.get_int("digest_hour_local")

# Track B rotation
TRACK_B_PLANS_PER_DAY = _t.get_int("track_b_plans_per_day")
TRACK_B_ROTATION_DAYS = _t.get_int("track_b_rotation_days")

# Priority buckets — re-exported so callers can still write `config.PriorityBucket`
# and `config.PRIORITY_BUCKETS`.
PRIORITY_BUCKETS: tuple[PriorityBucket, ...] = _t.priority_buckets

# Source tier — ordered list. When dedupe collapses N URLs for one story,
# slack_client picks the URL whose host matches earliest in this tuple.
SOURCE_TIER_1: tuple[str, ...] = _t.source_tiers


# --- Prompts ------------------------------------------------------------
#
# Both LLM prompts live as standalone markdown files in prompts/ rather than
# inline Python strings. Editing them is a copy edit, not a code change.
# Loaded once at import time; the file is read top-to-bottom verbatim and
# passed straight to the model — no headers stripped, no preprocessing.

def _load_prompt(name: str) -> str:
    """Load prompts/<name>.md as a string. Missing files raise at import."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


# System message for the ranker LLM call. Sets tone: "you are an editor for
# a VC firm." This is the single biggest lever for digest character.
RANKER_SYSTEM_PROMPT = _load_prompt("ranker_system")

# The S/A/B/C tier rubric the ranker sends to sonar-reasoning-pro inside its
# user prompt. Variable digest length: all Tier S, Tier A if room, Tier B
# only when a category would otherwise be empty, Tier C dropped.
MAGNITUDE_RUBRIC = _load_prompt("magnitude_rubric")


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
    if not TUNING_XLSX.exists():
        raise RuntimeError(
            f"Tuning file not found: {TUNING_XLSX}. Run "
            f"`python scripts/build_default_tuning_xlsx.py` to bootstrap defaults."
        )
    if not CONTENT_DIR.is_dir():
        raise RuntimeError(f"Content dir not found: {CONTENT_DIR}")
