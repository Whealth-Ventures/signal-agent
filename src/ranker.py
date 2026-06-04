"""Layer 4: magnitude-based editorial ranker.

Pulls every scored story from storage (no top-N cap), groups by priority_bucket,
sends the lot to Perplexity sonar-reasoning-pro along with the S/A/B/C rubric
from config.MAGNITUDE_RUBRIC, and assembles a digest with:

  - top_summary: the 5 highest-magnitude stories across all categories.
  - by_priority: per-category ordered lists, with the top_summary 5 pulled out
    (no repetition). Categories that end up empty are hidden.
  - other: stories with priority_bucket=None (RSS / Track B / voice / firm).

Selection rules: keep all Tier-S, keep Tier-A if room (subject to MAX_DIGEST_ITEMS),
keep Tier-B only when a priority category would otherwise be empty, drop Tier-C.
If that lands below TARGET_DIGEST_MIN, backfill with the best leftover Tier-B
stories (across all categories) so slow news days aren't thin.

If the LLM call fails or its output can't be parsed, falls back to score-order
inside each category and surfaces used_fallback=True to the caller.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
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
    # Within each group, highest score first (ranker still sees magnitude rubric
    # as primary signal, but score-order helps anchor close calls).
    for k in out:
        out[k].sort(key=lambda s: -s.relevance_score)
    return out


def build_prompt(grouped: dict[str, list[Story]]) -> str:
    lines = [
        "Tier each candidate story below using this rubric:",
        "",
        config.MAGNITUDE_RUBRIC,
        "",
        "For EACH story, return:",
        "  - `tier`: \"S\", \"A\", \"B\", or \"C\" (we will drop C)",
        f"  - `one_liner`: a single newsroom-headline sentence (max ~{ONE_LINER_MAX_CHARS} "
        "chars). State the WHAT in a punchy bullet style — Axios PM / Morning Brew. "
        "No commentary, no \"this matters because\". Do not editorialize.",
        "",
        "Return ONLY a JSON object (no markdown fences):",
        "{",
        '  "stories": [',
        '    {"story_id": "<id from below>", "tier": "S", '
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
) -> tuple[dict[str, tuple[Tier, str]], bool]:
    """Returns ({story_id → (tier, one_liner)}, used_fallback).

    Fallback triggers when nothing parseable came back. Stories present in
    `stories_by_id` but missing from the response are filled in by the
    selection logic later (defaulting to Tier A + fallback one-liner)."""
    parsed = _extract_json(response_text)
    out: dict[str, tuple[Tier, str]] = {}
    if not (parsed and isinstance(parsed.get("stories"), list)):
        return out, True

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
    return out, False


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
    enriched.sort(key=lambda x: (_TIER_RANK[x[1]], -x[0].relevance_score))
    return enriched


def _select(
    grouped: dict[str, list[Story]],
    decisions: dict[str, tuple[Tier, str]],
    max_total: int,
    target_min: int = 0,
) -> tuple[dict[str, list[RankedStory]], list[RankedStory]]:
    """Apply the keep-S / keep-A-if-room / B-only-if-empty rules to each
    category, then backfill toward `target_min` with the best leftover stories
    so slow news days aren't thin. Returns (priority_groups_by_key, other_list).

    Selection order:
      1. Keep all Tier-S, then Tier-A up to the global cap (max_total).
      2. For any priority category still empty, keep a single Tier-B so the
         section doesn't disappear ("Other" is exempt — empty Other just hides).
      3. If the running total is still below `target_min`, backfill from the
         leftover Tier-B/overflow-A pool (best tier→score first, across all
         categories including "Other") until the floor or the cap is reached.

    Tier-C is never selected (dropped in `_ordered_within_category`), so the
    floor only ever pulls in genuinely-ranked stories. `target_min=0` disables
    the backfill, giving the original pure-threshold behaviour.

    Note: the top_summary pull-out happens AFTER this — selected items stay
    in their categories here.
    """
    chosen_by_key: dict[str, list[tuple[Story, Tier, str]]] = {}
    bench: list[tuple[str, tuple[Story, Tier, str]]] = []  # (key, tup) backfill pool
    total = 0

    for key, stories in grouped.items():
        ordered = _ordered_within_category(stories, decisions)
        chosen: list[tuple[Story, Tier, str]] = []
        rest: list[tuple[Story, Tier, str]] = []

        # Pass 1: take all S, then A up to the global cap. Everything else
        # (Tier-B, plus any A that didn't fit) goes to the leftover pile.
        for tup in ordered:
            tier = tup[1]
            if tier == "S":
                chosen.append(tup)
                total += 1
            elif tier == "A" and total < max_total:
                chosen.append(tup)
                total += 1
            else:
                rest.append(tup)

        # Pass 2: if this priority category is still empty, promote a single
        # Tier-B to keep the section from disappearing.
        if not chosen and key != OTHER_KEY:
            b = next((t for t in rest if t[1] == "B"), None)
            if b is not None and total < max_total:
                chosen.append(b)
                rest.remove(b)
                total += 1

        chosen_by_key[key] = chosen
        for tup in rest:
            bench.append((key, tup))

    # Pass 3: backfill toward the floor with the best remaining stories,
    # ordered globally by (tier, -score) so the strongest leftovers win.
    if total < target_min and total < max_total and bench:
        bench.sort(key=lambda kt: (_TIER_RANK[kt[1][1]], -kt[1][0].relevance_score))
        for key, tup in bench:
            if total >= target_min or total >= max_total:
                break
            chosen_by_key[key].append(tup)
            total += 1

    by_priority: dict[str, list[RankedStory]] = {}
    other: list[RankedStory] = []
    for key, chosen in chosen_by_key.items():
        if not chosen:
            continue
        # Re-sort within the category so backfilled items land in tier/score order.
        chosen.sort(key=lambda x: (_TIER_RANK[x[1]], -x[0].relevance_score))
        ranked = [RankedStory(story=s, tier=t, one_liner=ol) for s, t, ol in chosen]
        if key == OTHER_KEY:
            other = ranked
        else:
            by_priority[key] = ranked

    return by_priority, other


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
    pool: list[tuple[int, float, int, str, RankedStory]] = []
    for key, items in by_priority.items():
        order = bucket_order.get(key, 999)
        for r in items:
            pool.append((
                _TIER_RANK[r.tier],
                -r.story.relevance_score,
                order,
                r.story.id,
                r,
            ))
    pool.sort(key=lambda x: x[:4])
    return [t[4] for t in pool[:n]]


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


def rank_stories(
    *,
    max_total: int = config.MAX_DIGEST_ITEMS,
    target_min: int = config.TARGET_DIGEST_MIN,
    top_summary_size: int = config.TOP_SUMMARY_SIZE,
    min_score: float = MIN_CANDIDATE_SCORE,
    candidate_pool_size: int = 60,
    recent_sent_window_days: int = RECENT_SENT_WINDOW_DAYS,
    conn: sqlite3.Connection | None = None,
    client: _RankerClient | None = None,
) -> RankingResult:
    start = time.monotonic()
    # Exclude stories already shipped in the last N days — otherwise high-
    # scoring evergreens keep winning the candidate pool and the digest
    # repeats itself.
    sent_urls = storage.recently_sent_urls(
        within_days=recent_sent_window_days, conn=conn,
    )
    candidates = storage.list_stories(
        min_score=min_score, limit=candidate_pool_size,
        exclude_urls=sent_urls, conn=conn,
    )
    if not candidates:
        return _empty_result(start)

    grouped = _group_for_prompt(candidates)
    stories_by_id = {st.id: st for st in candidates}

    # Call the LLM. If it fails or returns nothing parseable, we fall back to
    # treating everything as Tier A — the selection logic will still apply the
    # global cap and produce a sane digest.
    response_text = ""
    response_model: str | None = None
    response_cost = 0.0
    call_error: str | None = None
    if client is None:
        client = PerplexityClient()
    prompt = build_prompt(grouped)
    try:
        resp = client.complete(
            prompt,
            model=config.PERPLEXITY_MODEL_RANK,
            query_id="rank",
            system=_SYSTEM_PROMPT,
            timeout=config.HTTP_TIMEOUT_RANK_S,
        )
        response_text = resp.text
        response_model = resp.model
        response_cost = resp.estimated_cost_usd
    except (PerplexityCallFailed, RateLimitExceeded) as e:
        call_error = f"{type(e).__name__}: {e}"

    decisions, parse_fallback = parse_ranked(response_text, stories_by_id)
    used_fallback = bool(call_error) or parse_fallback

    if used_fallback:
        # The LLM topicality gate never ran, and relevance_score can't stand in
        # for topicality (the content corpus rewards VC/funding language, not
        # healthcare). Apply the deterministic healthcare lexicon so non-
        # healthcare stories can't ship in this degraded path. See topicality.py.
        dropped_non_healthcare = 0
        filtered: dict[str, list[Story]] = {}
        for key, sts in grouped.items():
            keep = [
                s for s in sts
                if is_healthcare(f"{s.canonical_title} {s.canonical_summary or ''}")
            ]
            dropped_non_healthcare += len(sts) - len(keep)
            filtered[key] = keep
        grouped = filtered
        _log({
            "step": "fallback_topicality_filter",
            "dropped_non_healthcare": dropped_non_healthcare,
            "reason": call_error or "unparseable_ranker_output",
        })

    by_priority, other = _select(grouped, decisions, max_total, target_min)
    top = _top_summary(by_priority, other, top_summary_size)
    by_priority_minus_top = _remove_promoted(by_priority, top)

    # Flat list in display order: top → by_priority (config order) → other.
    flat: list[RankedStory] = []
    flat.extend(top)
    for b in config.PRIORITY_BUCKETS:
        flat.extend(by_priority_minus_top.get(b.key, []))
    flat.extend(other)

    elapsed = round(time.monotonic() - start, 3)

    _log({
        "candidates_count": len(candidates),
        "top_summary_size": len(top),
        "by_priority_counts": {k: len(v) for k, v in by_priority_minus_top.items()},
        "other_count": len(other),
        "used_fallback": used_fallback,
        "model": response_model,
        "cost_usd": response_cost,
        "latency_ms": int(elapsed * 1000),
        "call_error": call_error,
        "response_text": response_text[:2000],
    })

    return RankingResult(
        top_summary=top,
        by_priority=by_priority_minus_top,
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
