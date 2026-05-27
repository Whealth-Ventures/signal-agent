"""Single source of truth for paths, env, and tunable constants.

Import is cheap: only loads .env, resolves paths, and ensures data/ subdirs exist.
No HTTP, no DB, no logging setup. Call check_env() from main.py at startup to
fail fast if required env vars are missing.
"""
from __future__ import annotations

import os
import re
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

SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL")
# Human-readable channel label, e.g. "#healthcare-signal". Optional — used only
# in the digests.recipients audit column and in logs; the webhook URL already
# pins which channel actually receives the post.
SLACK_CHANNEL_LABEL = _env("SLACK_CHANNEL_LABEL") or "(slack)"


# --- Constants ----------------------------------------------------------
#
# All tunable numbers live in this file. Modules import them from `config`
# rather than holding their own copies — keeps the "what shapes the output"
# surface to one place. See docs/TUNING.md for what each knob does.

# --- Budget --------------------------------------------------------------

MAX_PERPLEXITY_CALLS_PER_DAY = 60
DAILY_BUDGET_USD = 3.0


# --- Digest shape --------------------------------------------------------

# Ranker output is variable per category (see PRIORITY_BUCKETS + MAGNITUDE_RUBRIC).
# MAX_DIGEST_ITEMS is a sanity ceiling, not a target — typical days land 15–25.
MAX_DIGEST_ITEMS = 40
TOP_SUMMARY_SIZE = 5


# --- Dedup ---------------------------------------------------------------

# Single dedup window used by URL filter, ranker candidate filter, and the
# cross-day embedding similarity check. They were three constants before; in
# practice they should all be the same value (= "how far back does 'recent'
# go?"). Split them again if you ever need different windows per layer.
DEDUP_WINDOW_DAYS = 30

# Cross-day cosine threshold. Looser than the within-day cluster threshold
# below because different outlets covering the same event use different wording.
HISTORICAL_DEDUP_THRESHOLD = 0.80


# --- Scoring -------------------------------------------------------------

# Within-day clustering: signals whose canonical-text embeddings have cosine
# similarity above this threshold collapse into one story.
CLUSTER_SIMILARITY_THRESHOLD = 0.85

# How many content-corpus chunks to compare against when measuring a story's
# "does this sound like something the firm cares about" score.
TOP_K_CONTENT_SIMILARITY = 5

# Cap on the (title + summary) text fed into the embedding model. Longer
# summaries don't measurably improve clustering quality.
SUMMARY_TRUNCATE_FOR_EMBED = 400

# Boosters applied to the base content-similarity score. Inline numeric values
# so all tuning lives in one literal — no orphan SOMETHING_BOOST constants.
# "tier1_voice", "trusted_publication", and "firm_mention" are special-cased
# (matched against name/host sets, not regex) by scorer.compute_boosters.
BOOSTERS: dict[str, tuple[float, "re.Pattern[str] | None"]] = {
    "tier1_voice":         (0.10, None),
    "trusted_publication": (0.08, None),
    "firm_mention":        (0.08, None),
    "funding":    (0.05, re.compile(r"\b(raises?|series [a-d]|seed round|funding)\b", re.IGNORECASE)),
    "m_and_a":    (0.05, re.compile(r"\b(acquires?|acquisition|merges? with|m&a)\b", re.IGNORECASE)),
    "regulatory": (0.05, re.compile(r"\b(fda|cdsco|ema|approved|cleared)\b", re.IGNORECASE)),
    "product":    (0.03, re.compile(r"\b(launches?|unveils?|debuts?)\b", re.IGNORECASE)),
    "leadership": (0.03, re.compile(r"\b(appoints?|named|joins|hires?)\b", re.IGNORECASE)),
    "listicle":   (-0.10, re.compile(r"^\s*\d+\s+(best|top|essential|reasons|tips|ways)\b", re.IGNORECASE)),
    "opinion":    (-0.05, re.compile(r"^\s*(opinion|perspective|column):", re.IGNORECASE)),
}


# --- Ranker --------------------------------------------------------------

# Story candidates with relevance_score below this are silently dropped from
# the ranker's input pool. 0.0 means "let the ranker see everything."
MIN_CANDIDATE_SCORE = 0.0

# Tightest budget on the LLM's one-line headline (forces newsroom-punchy
# output, blocks paragraph creep).
ONE_LINER_MAX_CHARS = 120

# How much of each candidate's summary the prompt shows the ranker.
RANKER_SUMMARY_MAX_CHARS = 220


# --- Perplexity ----------------------------------------------------------

PERPLEXITY_MODEL_FETCH = "sonar-pro"
PERPLEXITY_MODEL_RANK = "sonar-reasoning-pro"
PERPLEXITY_RECENCY = "day"


# --- Embeddings ----------------------------------------------------------

EMBEDDING_MODEL = "text-embedding-3-small"


# --- HTTP ----------------------------------------------------------------

HTTP_TIMEOUT_S = 30
# sonar-reasoning-pro does extended chain-of-thought; 30s is too tight when the
# candidate prompt is large (timed out 4× in a row on --max-plans 20).
HTTP_TIMEOUT_RANK_S = 120
HTTP_MAX_RETRIES = 4
URL_VALIDATION_TIMEOUT_S = 10


# --- Schedule (digest sent at 10am IST) ---------------------------------

DIGEST_TZ = "Asia/Kolkata"
DIGEST_HOUR_LOCAL = 10


# --- Track B rotation ---------------------------------------------------

# Non-priority sub-buckets cycle across N days so all eventually get coverage
# without blowing past the daily call budget. With ~245 (sub-bucket × geo)
# combos, 18 picks/day fully covers in 14 days (18 * 14 = 252 >= 245).
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
