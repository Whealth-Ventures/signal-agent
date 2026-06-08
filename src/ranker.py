"""Layer 4: magnitude-based editorial ranker.

Pulls scored stories from storage (recency-ordered pool, no relevance gate),
drops anything that fails the healthcare topicality gate, then sends the lot to
the ranking LLM (Claude when ANTHROPIC_API_KEY is set, else Perplexity
sonar-reasoning-pro) along with the S/A/B/C rubric from config.MAGNITUDE_RUBRIC.
The LLM returns, per story: a tier, a one-line headline, AND the best-fit bucket
of the 8 priority buckets. The ranker assembles a digest with:

  - top_summary: the N highest-magnitude stories across all buckets.
  - by_priority: per-bucket ordered lists, with the top_summary pulled out
    (no repetition). Buckets that end up empty are hidden.

There is no 'Other' section: every story is forced into one of the 8 buckets
(LLM choice → its own Track-A bucket → a default), so `other` is always empty.

Ordering within a tier is by recency (relevance is no longer a ranking signal;
it survives only as a deep deterministic tiebreak). Selection is UNIFORM and
per-bucket: a fixed top_summary (TOP_SUMMARY_SIZE, default 5) of the highest-
magnitude stories across all buckets, then each of the 8 buckets shows up to
PER_BUCKET_MAX (default 2) of its remaining stories — so the body is consistent
day to day (1-2 per bucket) and a top story is never duplicated into its
category. Tier-C is dropped. A bucket is empty only when it genuinely has no
candidate (can't be manufactured).

If the LLM call fails or its output can't be parsed, falls back to recency-order
inside each bucket and surfaces used_fallback=True to the caller."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import config
import storage
from models import Story
from topicality import is_healthcare
from perplexity_client import (
    ChatResponse,
    PerplexityCallFailed,
    PerplexityClient,
    RateLimitExceeded,
)

# Every tunable in this module is sourced from config — see docs/TUNING.md.
# No pre-score-filter on candidates: the magnitude rubric handles tiering far
# better than a score threshold ever could, and a low-score story with the
# right magnitude (e.g. an FDA approval from a non-Tier-1 source) should still
# surface. candidate_pool_size below provides a sanity ceiling instead.
ONE_LINER_MAX_CHARS = config.ONE_LINER_MAX_CHARS
SUMMARY_MAX_CHARS_IN_PROMPT = config.RANKER_SUMMARY_MAX_CHARS
MIN_CANDIDATE_SCORE = config.MIN_CANDIDATE_SCORE

Tier = Literal["S", "A", "B", "C"]
_VALID_TIERS: tuple[Tier, ...] = ("S", "A", "B", "C")
_TIER_RANK: dict[Tier, int] = {"S": 0, "A": 1, "B": 2, "C": 3}

OTHER_KEY = "__other__"


def _valid_bucket_keys() -> set[str]:
    """The 8 priority-bucket keys — the only buckets a story may land in. Every
    story (except the top-summary, which spans buckets) must map to one of
    these; there is no 'Other' section anymore."""
    return {b.key for b in config.PRIORITY_BUCKETS}


def _default_bucket_key() -> str:
    """Catch-all for the rare story that has no LLM-assigned bucket AND no
    Track-A priority bucket (e.g. the ranker omitted it for an RSS/voice item).
    We force it into the first priority bucket rather than drop it — per the
    'never drop for lack of a bucket' rule. Logged when it happens."""
    return config.PRIORITY_BUCKETS[0].key


class _RankerClient(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        model: str = ...,
        recency: str | None = ...,
        query_id: str = ...,
        system: str | None = ...,
        timeout: float | None = ...,
    ) -> ChatResponse: ...


@dataclass(frozen=True)
class RankedStory:
    story: Story
    tier: Tier
    one_liner: str


@dataclass(frozen=True)
class RankingResult:
    top_summary: list[RankedStory]
    by_priority: dict[str, list[RankedStory]]   # PriorityBucket.key → ranked list
    other: list[RankedStory]
    candidates_count: int
    used_fallback: bool
    cost_usd: float
    elapsed_seconds: float
    # Convenience flat list (top_summary + by_priority + other, in display order)
    # so main.py can record everything in digest_stories without re-ordering.
    flat: tuple[RankedStory, ...] = field(default_factory=tuple)


_SYSTEM_PROMPT = config.RANKER_SYSTEM_PROMPT


# --- Prompt building ----------------------------------------------------

def _trim_summary(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) > SUMMARY_MAX_CHARS_IN_PROMPT:
        s = s[: SUMMARY_MAX_CHARS_IN_PROMPT - 1].rstrip() + "…"
    return s


def _priority_display(key: str | None) -> str:
    for b in config.PRIORITY_BUCKETS:
        if b.key == key:
            return b.display
    return "Other"


def _group_for_prompt(stories: list[Story]) -> dict[str, list[Story]]:
    """Group by priority_bucket.key (or OTHER_KEY). Preserves the PRIORITY_BUCKETS
    ordering for the priority groups; "Other" goes last."""
    out: dict[str, list[Story]] = {b.key: [] for b in config.PRIORITY_BUCKETS}
    out[OTHER_KEY] = []
    for st in stories:
        key = st.priority_bucket if st.priority_bucket in out else OTHER_KEY
        out[key].append(st)
    # Within each group, most recent first. Relevance is no longer a ranking
    # signal (see #5 / rank_stories) — it survives only as a last-resort
    # deterministic tiebreak when two stories share a publish time.
    for k in out:
        out[k].sort(key=lambda s: (-s.published_at.timestamp(), -s.relevance_score))
    return out


def _bucket_legend() -> str:
    """`key — display` lines for the 8 priority buckets, for the prompt."""
    return "\n".join(f"  - {b.key} — {b.display}" for b in config.PRIORITY_BUCKETS)


def build_prompt(grouped: dict[str, list[Story]]) -> str:
    bucket_keys = ", ".join(b.key for b in config.PRIORITY_BUCKETS)
    lines = [
        "Tier each candidate story below using this rubric:",
        "",
        config.MAGNITUDE_RUBRIC,
        "",
        "For EACH story, return:",
        "  - `tier`: \"S\", \"A\", \"B\", or \"C\" (we will drop C)",
        f"  - `one_liner`: a single newsroom-headline sentence (max ~{ONE_LINER_MAX_CHARS} "
        "chars). Lead with the SPECIFIC WHAT — name the company, the number, the "
        "action, the outcome. Punchy bullet style, Axios PM / Morning Brew. "
        "No commentary, no \"this matters because\", no vague abstractions. "
        "Do NOT prefix a geo tag like [IND]/[US] — that is added automatically.",
        "  - `bucket`: EXACTLY one of these keys — the single best fit for the "
        f"story: {bucket_keys}. Every story must get a bucket; if it doesn't "
        "obviously fit one, choose the CLOSEST. Buckets:",
        _bucket_legend(),
        "",
        "Examples of one_liner quality:",
        "  BAD  (vague, no specifics): "
        "\"Bridging the gap in pain awareness and treatment accessibility\"",
        "  GOOD (specific: who, how much, action, outcome): "
        "\"Paras Healthcare files ₹1,800 cr IPO with Sebi to fund expansion "
        "to 3,011 hospital beds by FY28\"",
        "",
        "Return ONLY a JSON object (no markdown fences):",
        "{",
        '  "stories": [',
        '    {"story_id": "<id from below>", "tier": "S", '
        '"bucket": "fda_regulatory", '
        '"one_liner": "FDA approves Eli Lilly\'s Kisunla for early Alzheimer\'s."},',
        "    ...",
        "  ]",
        "}",
        "",
    ]
    total = sum(len(v) for v in grouped.values())
    lines.append(f"Candidates ({total} total, grouped by category):")
    for key, stories in grouped.items():
        if not stories:
            continue
        section = _priority_display(key) if key != OTHER_KEY else "Other"
        lines.append("")
        lines.append(f"=== {section} ===")
        for st in stories:
            geo = st.geo or "-"
            lines.append(
                f"[id={st.id}  score={st.relevance_score:.3f}  geo={geo}]"
            )
            lines.append(f"  title: {st.canonical_title}")
            lines.append(f"  summary: {_trim_summary(st.canonical_summary)}")
            lines.append(f"  url: {st.canonical_url}")
    return "\n".join(lines)


# --- Response parsing ---------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _fallback_one_liner(story: Story) -> str:
    """Title trimmed to ONE_LINER_MAX_CHARS — used when the LLM didn't supply one."""
    title = (story.canonical_title or "").strip().replace("\n", " ")
    if len(title) > ONE_LINER_MAX_CHARS:
        title = title[: ONE_LINER_MAX_CHARS - 1].rstrip() + "…"
    return title


def _coerce_tier(v: object) -> Tier | None:
    if not isinstance(v, str):
        return None
    up = v.strip().upper()
    if up in _VALID_TIERS:
        return up  # type: ignore[return-value]
    return None


def parse_ranked(
    response_text: str,
    stories_by_id: dict[str, Story],
) -> tuple[dict[str, tuple[Tier, str]], dict[str, str], bool]:
    """Returns ({story_id → (tier, one_liner)}, {story_id → bucket_key},
    used_fallback).

    Fallback triggers when nothing parseable came back. Stories present in
    `stories_by_id` but missing from the response are filled in by the
    selection logic later (defaulting to Tier A + fallback one-liner). The
    bucket map only includes stories the model assigned a valid bucket key;
    the caller falls back to the story's own priority_bucket otherwise."""
    parsed = _extract_json(response_text)
    out: dict[str, tuple[Tier, str]] = {}
    buckets: dict[str, str] = {}
    if not (parsed and isinstance(parsed.get("stories"), list)):
        return out, buckets, True

    valid_buckets = _valid_bucket_keys()
    for entry in parsed["stories"]:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("story_id")
        if not isinstance(sid, str) or sid not in stories_by_id:
            continue
        tier = _coerce_tier(entry.get("tier"))
        if tier is None:
            continue
        ol = str(entry.get("one_liner") or "").strip().replace("\n", " ")
        if not ol:
            ol = _fallback_one_liner(stories_by_id[sid])
        out[sid] = (tier, ol[:ONE_LINER_MAX_CHARS])
        bucket = entry.get("bucket")
        if isinstance(bucket, str) and bucket.strip() in valid_buckets:
            buckets[sid] = bucket.strip()
    return out, buckets, False


# --- Selection ----------------------------------------------------------

def _ordered_within_category(
    stories: list[Story],
    decisions: dict[str, tuple[Tier, str]],
) -> list[tuple[Story, Tier, str]]:
    """Sort by tier (S < A < B), then by relevance_score desc. Stories without
    an LLM decision default to Tier A so they aren't silently dropped."""
    enriched: list[tuple[Story, Tier, str]] = []
    for st in stories:
        tier, ol = decisions.get(st.id, ("A", _fallback_one_liner(st)))
        if tier == "C":
            continue
        enriched.append((st, tier, ol))
    enriched.sort(key=lambda x: (
        _TIER_RANK[x[1]], -x[0].published_at.timestamp(), -x[0].relevance_score,
    ))
    return enriched


def _select(
    grouped: dict[str, list[Story]],
    decisions: dict[str, tuple[Tier, str]],
    *,
    per_bucket_max: int,
    top_summary_size: int,
) -> tuple[list[RankedStory], dict[str, list[RankedStory]]]:
    """Uniform per-bucket selection. Returns (top_summary, by_priority).

    Rule (see #2):
      1. Order each of the 8 buckets by tier (S<A<B), then recency. Tier-C
         dropped. (Every story is already forced into one of the 8 buckets, so
         there is no "Other" — OTHER_KEY, if present, is ignored here.)
      2. top_summary = the `top_summary_size` (default 5) highest-magnitude
         stories across ALL buckets. This is a flat highlight list — it is NOT
         broken out by category.
      3. Each bucket then shows up to `per_bucket_max` (default 2) of its
         stories that are NOT already in top_summary — so the body is uniform
         (1-2 per bucket) and a top story is never duplicated into its category.

    A bucket only ends up empty if it has no candidate at all (or its sole
    candidate was promoted to the top). With every story force-bucketed into the
    nearest of the 8, that is rare in practice — but it cannot be manufactured
    when a category genuinely has no news on a given day.
    """
    rs_by_key: dict[str, list[RankedStory]] = {}
    for b in config.PRIORITY_BUCKETS:
        ordered = _ordered_within_category(grouped.get(b.key, []), decisions)
        if ordered:
            rs_by_key[b.key] = [
                RankedStory(story=s, tier=t, one_liner=ol) for s, t, ol in ordered
            ]

    top = _top_summary(rs_by_key, [], top_summary_size)
    top_ids = {r.story.id for r in top}

    by_priority: dict[str, list[RankedStory]] = {}
    for b in config.PRIORITY_BUCKETS:
        body = [r for r in rs_by_key.get(b.key, []) if r.story.id not in top_ids]
        body = body[:per_bucket_max]
        if body:
            by_priority[b.key] = body

    return top, by_priority


def _top_summary(
    by_priority: dict[str, list[RankedStory]],
    other: list[RankedStory],
    n: int,
) -> list[RankedStory]:
    """Top n stories across all categories, ranked by (tier, -score). Tiebreak
    by priority order from config.PRIORITY_BUCKETS, then by story id (deterministic).
    Excludes "Other" — top_summary is for the headline news; Other is long-tail."""
    bucket_order: dict[str, int] = {
        b.key: i for i, b in enumerate(config.PRIORITY_BUCKETS)
    }
    pool: list[tuple[int, float, int, float, str, RankedStory]] = []
    for key, items in by_priority.items():
        order = bucket_order.get(key, 999)
        for r in items:
            pool.append((
                _TIER_RANK[r.tier],
                -r.story.published_at.timestamp(),   # recency primary (post-#5)
                order,
                -r.story.relevance_score,            # deep deterministic tiebreak
                r.story.id,
                r,
            ))
    pool.sort(key=lambda x: x[:5])
    return [t[5] for t in pool[:n]]


def _remove_promoted(
    by_priority: dict[str, list[RankedStory]],
    promoted: list[RankedStory],
) -> dict[str, list[RankedStory]]:
    """Strip the `promoted` items from `by_priority`; drop categories that
    become empty. Per the locked Slack format, top-summary items don't repeat
    in their category sections."""
    promoted_ids = {r.story.id for r in promoted}
    out: dict[str, list[RankedStory]] = {}
    for key, items in by_priority.items():
        kept = [r for r in items if r.story.id not in promoted_ids]
        if kept:
            out[key] = kept
    return out


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"ranker_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# --- Orchestrator -------------------------------------------------------

RECENT_SENT_WINDOW_DAYS = config.DEDUP_WINDOW_DAYS


def _build_ranker_client() -> tuple[_RankerClient, str]:
    """Pick the ranking vendor. Claude when RANKER_PROVIDER=='anthropic' AND a
    key is set; otherwise Perplexity sonar-reasoning-pro. Returns (client,
    model_to_use)."""
    if config.RANKER_PROVIDER == "anthropic" and config.ANTHROPIC_API_KEY:
        from anthropic_client import AnthropicClient
        return AnthropicClient(), config.ANTHROPIC_MODEL_RANK
    return PerplexityClient(), config.PERPLEXITY_MODEL_RANK


def _effective_bucket(
    story: Story, llm_buckets: dict[str, str], valid: set[str], default: str,
) -> str:
    """The bucket a story lands in: LLM's choice → its own Track-A bucket →
    the catch-all default. Guarantees every story maps to one of the 8."""
    b = llm_buckets.get(story.id)
    if b in valid:
        return b
    if story.priority_bucket in valid:
        return story.priority_bucket
    return default


def rank_stories(
    *,
    per_bucket_max: int = config.PER_BUCKET_MAX,
    top_summary_size: int = config.TOP_SUMMARY_SIZE,
    min_score: float = MIN_CANDIDATE_SCORE,
    candidate_pool_size: int = 120,
    recent_sent_window_days: int = RECENT_SENT_WINDOW_DAYS,
    conn: sqlite3.Connection | None = None,
    client: _RankerClient | None = None,
) -> RankingResult:
    start = time.monotonic()
    # Exclude stories already shipped in the last N days — otherwise evergreens
    # keep winning the candidate pool and the digest repeats itself. The pool is
    # ordered by recency, not relevance (see #5): relevance no longer gates or
    # ranks anything; magnitude tiering + the topicality gate do the work.
    sent_urls = storage.recently_sent_urls(
        within_days=recent_sent_window_days, conn=conn,
    )
    pool = storage.list_stories(
        min_score=min_score, limit=candidate_pool_size,
        exclude_urls=sent_urls, order_by_recency=True, conn=conn,
    )
    # Topicality gate runs on EVERY path now (not just the degraded fallback):
    # a story must read as healthcare to be a candidate at all. See topicality.py.
    candidates = [
        s for s in pool
        if is_healthcare(f"{s.canonical_title} {s.canonical_summary or ''}")
    ]
    dropped_non_healthcare = len(pool) - len(candidates)
    if dropped_non_healthcare:
        _log({"step": "topicality_gate", "dropped_non_healthcare": dropped_non_healthcare})
    if not candidates:
        return _empty_result(start)

    grouped_for_prompt = _group_for_prompt(candidates)
    stories_by_id = {st.id: st for st in candidates}

    # Call the LLM. If it fails or returns nothing parseable, we fall back to
    # treating everything as Tier A — the selection logic still applies the
    # global cap and produces a sane digest. Any exception is treated as a
    # fallback so the digest always ships regardless of vendor.
    response_text = ""
    response_model: str | None = None
    response_cost = 0.0
    call_error: str | None = None
    rank_model = config.PERPLEXITY_MODEL_RANK
    if client is None:
        client, rank_model = _build_ranker_client()
    prompt = build_prompt(grouped_for_prompt)
    try:
        resp = client.complete(
            prompt,
            model=rank_model,
            query_id="rank",
            system=_SYSTEM_PROMPT,
            timeout=config.HTTP_TIMEOUT_RANK_S,
        )
        response_text = resp.text
        response_model = resp.model
        response_cost = resp.estimated_cost_usd
    except Exception as e:  # any vendor error → degrade to fallback, still ship
        call_error = f"{type(e).__name__}: {e}"

    decisions, llm_buckets, parse_fallback = parse_ranked(response_text, stories_by_id)
    used_fallback = bool(call_error) or parse_fallback

    # Force every candidate into one of the 8 buckets (no 'Other' section). The
    # LLM picks the best fit; Track-A stories fall back to their own bucket; the
    # rare straggler lands in the default bucket rather than being dropped.
    valid = _valid_bucket_keys()
    default_bucket = _default_bucket_key()
    bucketed: list[Story] = []
    default_assigned = 0
    for st in candidates:
        eff = _effective_bucket(st, llm_buckets, valid, default_bucket)
        if eff == default_bucket and llm_buckets.get(st.id) not in valid \
                and st.priority_bucket not in valid:
            default_assigned += 1
        bucketed.append(replace(st, priority_bucket=eff))
    if default_assigned:
        _log({"step": "bucket_default_assigned", "count": default_assigned,
              "default_bucket": default_bucket})

    grouped = _group_for_prompt(bucketed)
    # Uniform per-bucket selection: fixed top-5 highlights + up to per_bucket_max
    # (1-2) per bucket, no 'Other' section.
    top, by_priority = _select(
        grouped, decisions,
        per_bucket_max=per_bucket_max, top_summary_size=top_summary_size,
    )
    other: list[RankedStory] = []  # no 'Other' section — every story is bucketed

    # Flat list in display order: top → by_priority (config order).
    flat: list[RankedStory] = []
    flat.extend(top)
    for b in config.PRIORITY_BUCKETS:
        flat.extend(by_priority.get(b.key, []))

    elapsed = round(time.monotonic() - start, 3)

    _log({
        "candidates_count": len(candidates),
        "top_summary_size": len(top),
        "by_priority_counts": {k: len(v) for k, v in by_priority.items()},
        "empty_buckets": [b.key for b in config.PRIORITY_BUCKETS
                          if b.key not in by_priority],
        "other_count": 0,
        "default_bucket_assigned": default_assigned,
        "used_fallback": used_fallback,
        "model": response_model,
        "cost_usd": response_cost,
        "latency_ms": int(elapsed * 1000),
        "call_error": call_error,
        "response_text": response_text[:2000],
    })

    return RankingResult(
        top_summary=top,
        by_priority=by_priority,
        other=other,
        candidates_count=len(candidates),
        used_fallback=used_fallback,
        cost_usd=response_cost,
        elapsed_seconds=elapsed,
        flat=tuple(flat),
    )


def _empty_result(start: float) -> RankingResult:
    elapsed = round(time.monotonic() - start, 3)
    _log({"candidates_count": 0, "used_fallback": False, "latency_ms": int(elapsed * 1000)})
    return RankingResult(
        top_summary=[], by_priority={}, other=[],
        candidates_count=0, used_fallback=False,
        cost_usd=0.0, elapsed_seconds=elapsed, flat=(),
    )
