"""Single source of truth for paths, env, and tunable constants.

Import is cheap: only loads .env, resolves paths, and ensures data/ subdirs exist.
No HTTP, no DB, no logging setup. Call check_env() from main.py at startup to
fail fast if required env vars are missing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
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
DAILY_BUDGET_USD = 3.0

# Digest shape
# Ranker output is variable per category (see PRIORITY_BUCKETS + MAGNITUDE_RUBRIC).
# MAX_DIGEST_ITEMS is a sanity ceiling, not a target — typical days land 15–25.
MAX_DIGEST_ITEMS = 40
TOP_SUMMARY_SIZE = 5

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

# Track B rotation — non-priority sub-buckets cycle across N days so all
# eventually get coverage without blowing past the daily call budget. With
# ~245 (sub-bucket × geo) combos, 18 picks/day fully covers in 14 days
# (18 * 14 = 252 >= 245).
TRACK_B_PLANS_PER_DAY = 18
TRACK_B_ROTATION_DAYS = 14


# --- Priority buckets ---------------------------------------------------

@dataclass(frozen=True)
class PriorityBucket:
    """A daily-tracked category. Maps to one or more sub-buckets in keywords.xlsx.

    `geos` controls which (geo, bucket) plans the query_planner emits:
      - ("India", "US") → two plans, one India-filtered, one US-filtered
      - ("US",)         → one US plan
      - ("Global",)     → one plan querying across all geos (no Geo filter)
    """
    key: str                          # slug used in storage + ranker output
    display: str                      # human-readable label for Slack
    sub_buckets: tuple[str, ...]      # exact names from Master Keywords sheet
    geos: tuple[str, ...]


PRIORITY_BUCKETS: tuple[PriorityBucket, ...] = (
    PriorityBucket(
        key="venture_ipo",
        display="Venture & IPO",
        sub_buckets=("Venture and IPO", "Capital and deal types"),
        geos=("India", "US"),
    ),
    PriorityBucket(
        key="pe_strategics",
        display="PE & Strategics",
        sub_buckets=("Private equity and strategics",),
        geos=("India", "US"),
    ),
    PriorityBucket(
        key="hospital_ma",
        display="Hospital & Health System M&A",
        sub_buckets=("Hospital and health system M&A",),
        geos=("India", "US"),
    ),
    PriorityBucket(
        key="mso_rollups",
        display="Physician Practice & MSO Roll-ups",
        sub_buckets=("Physician practice and MSO roll-ups",),
        geos=("US",),
    ),
    PriorityBucket(
        key="fda_regulatory",
        display="FDA & Regulatory",
        sub_buckets=("FDA, regulatory and approvals",),
        geos=("India", "US"),
    ),
    PriorityBucket(
        key="hot_tas",
        display="Phase 3 / Hot Therapeutic Areas",
        sub_buckets=("Hot therapeutic areas",),
        geos=("Global",),
    ),
    PriorityBucket(
        key="us_medicare",
        display="US Medicare",
        sub_buckets=("US Medicare policy and reform",),
        geos=("US",),
    ),
    # AI in Healthcare emits two plans (ventures/fundraises + clinical/partnerships)
    # — see query_planner._build_ai_plans. Both share this single PriorityBucket
    # for storage + Slack grouping.
    PriorityBucket(
        key="ai_healthcare",
        display="AI in Healthcare",
        sub_buckets=(
            "Generative AI and LLMs in healthcare",
            "AI scribes and ambient documentation",
            "Revenue cycle and administrative AI",
        ),
        geos=("Global",),
    ),
)


# --- Magnitude rubric ---------------------------------------------------

# The ranker sends this rubric to sonar-reasoning-pro inside its system prompt.
# Output is variable per category: all Tier S, Tier A if room, Tier B only when
# the category is otherwise empty. Tier C is dropped.
MAGNITUDE_RUBRIC = """\
TIER S — must include (at least one per category if any present):
  - FDA / CDSCO / EMA approval, rejection, or major label change
  - Phase 3 trial readout (positive or negative)
  - M&A >$100M, IPO, public listing, S-1/DRHP filing
  - Major CMS / Medicare rule change, federal policy shift
  - Bankruptcy / large layoff / hospital closure at top-50 player

TIER A — include if room:
  - Funding round >=$10M (Series A/B/C/D)
  - PE platform deal, growth equity round, take-private
  - C-suite move at a top-50 player
  - Substantial product launch with a named anchor customer
  - Big-tech entrant moves (Amazon Health, Apple Health, Google Health, etc.)

TIER B — include only when the category would otherwise be empty:
  - Sub-$10M funding, partnerships, single-state geo expansions
  - Leadership move at smaller player
  - Conference / event announcements

TIER C — DROP:
  - Listicles, opinion columns, hot takes, "10 best…" pieces
  - Single-clinic openings, hiring updates, routine PR
  - Reposts, brief reactions, congratulatory replies"""


# --- Source tier (for picking the best link when a story has duplicates) ---

# Ordered list — when dedupe collapses N URLs for one story, slack_client picks
# the URL whose host matches earliest in this tuple. Exact host match preferred;
# falls back to suffix match (so www.bloomberg.com matches bloomberg.com).
# If nothing matches, the first signal URL is used.
SOURCE_TIER_1: tuple[str, ...] = (
    # US — finance / business / general
    "bloomberg.com",
    "reuters.com",
    "wsj.com",
    "nytimes.com",
    "ft.com",
    "axios.com",
    "cnbc.com",
    "forbes.com",
    # US — healthcare-specific
    "statnews.com",
    "endpts.com",
    "biopharmadive.com",
    "modernhealthcare.com",
    "fiercehealthcare.com",
    "fiercebiotech.com",
    "fiercepharma.com",
    "healthcaredive.com",
    "medcitynews.com",
    "beckershospitalreview.com",
    "healthcareitnews.com",
    "healthaffairs.org",
    # India — finance / business / general
    "economictimes.indiatimes.com",
    "livemint.com",
    "business-standard.com",
    "moneycontrol.com",
    "thehindubusinessline.com",
    "financialexpress.com",
    "thehindu.com",
    "indianexpress.com",
    # India — healthcare-specific
    "health.economictimes.indiatimes.com",
    "biospectrumindia.com",
    "expresshealthcare.in",
    "pharmabiz.com",
    # India — tech / startup news
    "inc42.com",
    "yourstory.com",
    "entrackr.com",
    "the-ken.com",
)


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
