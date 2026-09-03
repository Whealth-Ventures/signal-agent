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
from urllib.parse import parse_qsl, urlparse

import numpy as np

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

# Below this share of candidates carrying an LLM decision, the run counts as
# degraded — see the `partial` check in rank_stories.
MIN_DECISION_COVERAGE = 0.6

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
    """Catch-all for a story with no LLM-assigned bucket AND no Track-A
    priority bucket (e.g. the ranker omitted it for an RSS/voice item). We
    force it into a bucket rather than drop it — per the 'never drop for lack
    of a bucket' rule. Logged when it happens.

    Read from the `default_bucket` tunable so the catch-all is a deliberate
    choice. It used to be `PRIORITY_BUCKETS[0]`, i.e. whichever bucket happened
    to sit in row 1 of the sheet: that is `venture_ipo`, which is why a STAT
    opinion piece on the diabetes association shipped under "Venture & IPO" on
    2 Sept 2026. An unset or unknown value keeps the old first-bucket
    behaviour, so this is safe before the sheet is updated.
    """
    configured = (config.DEFAULT_BUCKET or "").strip()
    if configured and configured in _valid_bucket_keys():
        return configured
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
    # EVERY ranked candidate (Tier-C excluded), grouped by bucket and ordered —
    # not just the ~21 that made this cut. `top_summary`/`by_priority` are one
    # geo-blind selection out of these; `filter_by_geo` needs the full lists to
    # make a second, geo-scoped selection. Without it a channel could only ever
    # see the stories that survived the geo-blind per-bucket cap, so the US
    # digest lost 7 of 8 category sections to Indian stories that outranked its
    # own. Empty on a hand-built result; filter_by_geo degrades gracefully.
    ranked_by_bucket: dict[str, tuple[RankedStory, ...]] = field(
        default_factory=dict,
    )


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
    # Within each group, highest relevance first, recency as the tiebreak —
    # same order the digest itself uses (see _ordered_within_category). This
    # ordering decides which stories the model reaches first, so under a
    # truncated response it is the strongest candidates that get a tier,
    # bucket and geo rather than merely the newest.
    for k in out:
        out[k].sort(key=lambda s: (-s.relevance_score, -s.published_at.timestamp()))
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
        "  - `geo`: EXACTLY \"India\", \"US\" or \"Global\" — where the NEWS "
        "happened, judged from the story itself. This is NOT the nationality of "
        "the publication that reported it: an Indian outlet covering a US "
        "hospital merger is \"US\", and a US outlet covering an Indian funding "
        "round is \"India\". Use \"Global\" only when the news genuinely spans "
        "regions or has no single home — a multinational drug approval, a "
        "worldwide product launch, cross-border research. The `geo=` value shown "
        "against each candidate below is a weak hint from where we found it; "
        "correct it when the story says otherwise.",
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
        '"bucket": "fda_regulatory", "geo": "US", '
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


VALID_GEOS = ("India", "US", "Global")


def _coerce_geo(v: object) -> str | None:
    """Model's `geo` → one of India / US / Global, or None if unusable."""
    if not isinstance(v, str):
        return None
    g = v.strip().lower()
    for valid in VALID_GEOS:
        if g == valid.lower():
            return valid
    return None


def parse_ranked(
    response_text: str,
    stories_by_id: dict[str, Story],
) -> tuple[dict[str, tuple[Tier, str]], dict[str, str], dict[str, str], bool]:
    """Returns ({story_id → (tier, one_liner)}, {story_id → bucket_key},
    {story_id → geo}, used_fallback).

    Fallback triggers when nothing parseable came back. Stories present in
    `stories_by_id` but missing from the response are filled in by the
    selection logic later (defaulting to Tier A + fallback one-liner). The
    bucket and geo maps only include stories the model gave a valid value for;
    the caller falls back to the story's own priority_bucket / geo otherwise."""
    parsed = _extract_json(response_text)
    out: dict[str, tuple[Tier, str]] = {}
    buckets: dict[str, str] = {}
    geos: dict[str, str] = {}
    if not (parsed and isinstance(parsed.get("stories"), list)):
        return out, buckets, geos, True

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
        geo = _coerce_geo(entry.get("geo"))
        if geo:
            geos[sid] = geo
    return out, buckets, geos, False


# --- Selection ----------------------------------------------------------

def _ordered_within_category(
    stories: list[Story],
    decisions: dict[str, tuple[Tier, str]],
) -> list[tuple[Story, Tier, str]]:
    """Sort by tier (S < A < B), then relevance_score desc, then recency.

    Stories without an LLM decision default to Tier A so they aren't silently
    dropped.

    Score before recency, deliberately (reverses the earlier recency-primary
    rule). Signals are already windowed to the last 24h at fetch time, so
    publish time barely discriminates inside a digest, while relevance_score
    carries the corpus-similarity and booster signal. With recency primary and
    every story flattened to Tier A on the degraded path, the digest became a
    plain recency feed: on 2 Sept 2026 that dropped an Ultrahuman funding
    exclusive scored 0.68 and a $22M Series B scored 0.72 — the day's top two
    stories — because lower-scored items published a few hours later took the
    per-bucket slots. Recency stays as the tiebreak.
    """
    enriched: list[tuple[Story, Tier, str]] = []
    for st in stories:
        tier, ol = decisions.get(st.id, ("A", _fallback_one_liner(st)))
        if tier == "C":
            continue
        enriched.append((st, tier, ol))
    enriched.sort(key=lambda x: (
        _TIER_RANK[x[1]], -x[0].relevance_score, -x[0].published_at.timestamp(),
    ))
    return enriched


def _select(
    grouped: dict[str, list[Story]],
    decisions: dict[str, tuple[Tier, str]],
    *,
    per_bucket_max: int,
    top_summary_size: int,
) -> tuple[list[RankedStory], dict[str, list[RankedStory]],
           dict[str, list[RankedStory]]]:
    """Uniform per-bucket selection.

    Returns (top_summary, by_priority, all_ranked_by_bucket) — the third is
    every ranked candidate grouped by bucket, kept so filter_by_geo can
    re-select per channel instead of being stuck with this geo-blind cut.

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
    top, by_priority = _split_top_and_bodies(
        rs_by_key, per_bucket_max=per_bucket_max,
        top_summary_size=top_summary_size,
    )
    return top, by_priority, rs_by_key


def _rank_within_bucket(r: RankedStory) -> tuple[int, float, float]:
    """Same ordering as _ordered_within_category, for already-ranked stories."""
    return (
        _TIER_RANK[r.tier],
        -r.story.relevance_score,
        -r.story.published_at.timestamp(),
    )


def _split_top_and_bodies(
    rs_by_key: dict[str, list[RankedStory]],
    *,
    per_bucket_max: int,
    top_summary_size: int,
) -> tuple[list[RankedStory], dict[str, list[RankedStory]]]:
    """Steps 2 and 3 of the rule above, over already-ranked stories.

    Split out so `filter_by_geo` can re-run the same selection on a geo-scoped
    set instead of filtering an already-made one.
    """
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
    by recency, then priority order from config.PRIORITY_BUCKETS, then story id
    (deterministic).
    Excludes "Other" — top_summary is for the headline news; Other is long-tail.

    Score sits ahead of recency for the same reason as
    _ordered_within_category — see its docstring."""
    bucket_order: dict[str, int] = {
        b.key: i for i, b in enumerate(config.PRIORITY_BUCKETS)
    }
    pool: list[tuple[int, float, float, int, str, RankedStory]] = []
    for key, items in by_priority.items():
        order = bucket_order.get(key, 999)
        for r in items:
            pool.append((
                _TIER_RANK[r.tier],
                -r.story.relevance_score,            # magnitude primary
                -r.story.published_at.timestamp(),   # then recency
                order,
                r.story.id,                          # deterministic tiebreak
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


def _build_ranker_client(
    fallback: "_RankerClient | None" = None,
) -> tuple[_RankerClient, str]:
    """Pick the ranking vendor. Claude when RANKER_PROVIDER=='anthropic' AND a
    key is set; otherwise Perplexity sonar-reasoning-pro. Returns (client,
    model_to_use).

    `fallback` is the caller's already-built Perplexity client (main.py hands
    over its fetch client). It is used ONLY on the Perplexity path, and only so
    the per-(date, geo) budget counter stays shared — a client built here would
    carry no geo scope and would bill against the wrong log file.

    It must never win over an explicit anthropic provider: it used to, because
    rank_stories() called this only when no client was passed, which silently
    pinned every production run to Perplexity and left the Claude path dead.
    """
    if config.RANKER_PROVIDER == "anthropic" and config.ANTHROPIC_API_KEY:
        from anthropic_client import AnthropicClient
        return AnthropicClient(), config.ANTHROPIC_MODEL_RANK
    return fallback or PerplexityClient(), config.PERPLEXITY_MODEL_RANK


_PUNCT_RE = re.compile(r"[^a-z0-9]+")

# Query parameters that identify a campaign, not an article. Everything else is
# assumed to be load-bearing: on plenty of trade publishers the article id
# lives ONLY in the query (pharmabiz.com/NewsDetails.aspx?aid=…), so dropping
# the whole query string would collapse every article on the site into one.
_TRACKING_PARAMS = frozenset({
    "cid", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "referrer",
    "source", "spm", "yclid",
})


def _norm_query(raw: str) -> str:
    """Query string minus tracking noise, sorted for stability."""
    if not raw:
        return ""
    kept = [
        (k, v) for k, v in parse_qsl(raw, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
        and not k.lower().startswith("utm_")
    ]
    if not kept:
        return ""
    return "?" + "&".join(f"{k.lower()}={v}" for k, v in sorted(kept))


def _identity_keys(story: Story) -> list[str]:
    """Every key by which this story counts as "the same news" as another.

    A publisher can serve one article under several URLs — BioSpectrum India
    puts the same piece at /news/16/28410/<slug> and /news/101/28410/<slug> —
    so URL equality alone misses it, and the two copies then eat two slots in
    the same digest section. Three keys, any of which is a match:

      t:<title>     normalised title (case, punctuation and spacing removed)
      u:<url>       host + path + query, minus tracking params. The query is
                    KEPT because some publishers put the article id there and
                    nowhere else; dropping it collapsed three unrelated
                    pharmabiz.com articles into one.
      s:<host>/<slug>  host + last path segment, which survives the differing
                    middle segments above. Requires 12+ chars AND a hyphen, so
                    it fires on a real slug and not on /feed, /news, /2026 or
                    a script name like /NewsDetails.aspx.
    """
    keys: list[str] = []
    title = _PUNCT_RE.sub(" ", (story.canonical_title or "").lower()).strip()
    if title:
        keys.append(f"t:{title}")

    p = urlparse(story.canonical_url or "")
    host = (p.hostname or "").lower().removeprefix("www.")
    path = (p.path or "").rstrip("/")
    if host:
        keys.append(f"u:{host}{path.lower()}{_norm_query(p.query)}")
        segments = [seg for seg in path.split("/") if seg]
        if segments:
            last = segments[-1].lower()
            if len(last) >= 12 and "-" in last:
                keys.append(f"s:{host}/{last}")
    return keys


def collapse_duplicates(stories: list[Story]) -> tuple[list[Story], int]:
    """Drop repeat tellings of the same story, keeping the best-scoring one.

    Runs on the CANDIDATE pool, before bucketing and selection, for two
    reasons: the freed slot goes to the next real story instead of being lost,
    and the ranker prompt never shows the model two copies to tier separately
    (on 3 Sept 2026 it tiered both, and AIG Hospitals and Luma Fertility each
    shipped twice, costing 4 of 15 slots).

    Every drop is logged with the story it matched and the key that matched it,
    so a wrong collapse is findable after the fact instead of being a count.

    Returns (survivors in their original order, dropped_count).
    """
    best_first = sorted(stories, key=lambda s: -s.relevance_score)
    seen: dict[str, Story] = {}
    keep: set[str] = set()
    for st in best_first:
        keys = _identity_keys(st)
        hit = next((k for k in keys if k in seen), None)
        if hit is not None:
            kept = seen[hit]
            _log({
                "step": "duplicate_dropped",
                "matched_on": hit.split(":", 1)[0],
                "key": hit,
                "dropped": {
                    "id": st.id, "score": round(st.relevance_score, 4),
                    "title": st.canonical_title, "url": st.canonical_url,
                },
                "kept": {
                    "id": kept.id, "score": round(kept.relevance_score, 4),
                    "title": kept.canonical_title, "url": kept.canonical_url,
                },
            })
            continue
        for k in keys:
            seen[k] = st
        keep.add(st.id)
    survivors = [st for st in stories if st.id in keep]
    return survivors, len(stories) - len(survivors)


def collapse_near_duplicates(
    stories: list[Story],
    embeddings: dict[str, list[float]],
    *,
    threshold: float = config.CLUSTER_SIMILARITY_THRESHOLD,
) -> tuple[list[Story], int]:
    """Collapse stories that are the same news told by different publications.

    `collapse_duplicates` catches one article served under several URLs. It
    cannot catch two outlets reporting the same event, because the titles, URLs
    and slugs all differ. Only the embeddings show it.

    Observed 3 Sept 2026: MediBuddy's appointment of Shalabh Shrivastava was
    reported by Entrackr (2 Sept), BioSpectrum (3 Sept 02:22) and Express
    Healthcare (3 Sept 08:45), producing three separate stories. Two of them
    took two of the five headline slots in the same digest. `scorer` already
    clusters within one scoring run but never re-clusters against earlier days,
    and those three arrived in three different runs.

    **Greedy leader selection, deliberately not single-linkage.** Highest
    relevance_score first; each leader is kept and absorbs every remaining
    story that clears `threshold` against IT. So a dropped story is always a
    near-duplicate of the specific story that replaced it.

    Connected components would be wrong here. Over a 30-day pool a chain of
    near-threshold neighbours merges things that share nothing: six stories
    each 0.86 to the next collapse to one survivor whose endpoints sit at
    cosine **-0.89**. The real case needs no transitivity anyway — both
    MediBuddy pairs (0.8924 and 0.8831) are measured against Entrackr, which
    is also the score winner at 0.6196.

    Stories with no stored embedding are always kept — never drop something we
    couldn't compare.
    """
    if len(stories) < 2:
        return stories, 0

    have = [s for s in stories if s.id in embeddings]
    if len(have) < 2:
        return stories, 0

    # Strongest first, so the leader of each group is the story that survives.
    order = sorted(have, key=lambda s: (-s.relevance_score, s.id))
    vecs = np.asarray([embeddings[s.id] for s in order], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    n = len(order)
    alive = [True] * n
    keep: set[str] = {s.id for s in stories if s.id not in embeddings}
    for i in range(n):
        if not alive[i]:
            continue
        keep.add(order[i].id)
        sims = vecs @ vecs[i]
        for j in range(i + 1, n):
            if not alive[j] or float(sims[j]) < threshold:
                continue
            alive[j] = False
            _log({
                "step": "near_duplicate_dropped",
                "threshold": threshold,
                "similarity": round(float(sims[j]), 4),
                "dropped": {
                    "id": order[j].id,
                    "score": round(order[j].relevance_score, 4),
                    "title": order[j].canonical_title,
                    "url": order[j].canonical_url,
                },
                "kept": {
                    "id": order[i].id,
                    "score": round(order[i].relevance_score, 4),
                    "title": order[i].canonical_title,
                    "url": order[i].canonical_url,
                },
            })
    survivors = [s for s in stories if s.id in keep]
    return survivors, len(stories) - len(survivors)


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


def _effective_geo(story: Story, llm_geos: dict[str, str]) -> str | None:
    """Where the news happened: the ranker's judgement → what we inherited.

    The ranker reads the story, so it is the only component that can tell an
    Indian outlet's report on a US hospital merger from Indian news. What we
    inherit is a proxy and a poor one in both directions:

      - a Perplexity signal takes the geography of the PLAN that found it, so
        anything found by a Global plan is Global whatever it says;
      - an RSS signal takes its PUBLICATION's geography, so Digital Health
        News reporting on Sanford/North Memorial in Minnesota came out India,
        and medicaldialogues.in on Novartis halting CAR-T trials came out
        India (both observed 3 Sept 2026).

    So the proxy is the fallback, never the answer, and None still means
    "unknown" — rendered [GLOBAL] and kept by both channels, as before.
    """
    return llm_geos.get(story.id) or story.geo


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
    """`client` is a FALLBACK, not an override — see _build_ranker_client().
    When RANKER_PROVIDER=='anthropic' and a key is set, ranking goes to Claude
    and this client is ignored. Tests injecting a fake must therefore pin the
    provider (patch config.ANTHROPIC_API_KEY to "") or they will reach the real
    API on any machine that has the key in its .env."""
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

    # The save-time blocklist is forward-only: stories ingested before a host
    # was blocked stay in the 30-day pool and remain eligible. Re-check here so
    # adding a host takes effect on the next run, not in a month.
    still_blocked = [s for s in candidates if storage.is_blocked_url(s.canonical_url)]
    if still_blocked:
        _log({
            "step": "blocked_domain_candidates_dropped",
            "count": len(still_blocked),
            "urls": [s.canonical_url for s in still_blocked[:20]],
        })
        blocked_ids = {s.id for s in still_blocked}
        candidates = [s for s in candidates if s.id not in blocked_ids]

    # Same news, told twice, must never occupy two slots. Two passes: exact
    # identity (one article, several URLs), then embedding similarity (two
    # publications, same event — different titles, so keys can't see it).
    #
    # Both guarded. This module's contract is that the digest always ships, and
    # `run_pipeline` is try/finally with no except, so anything raised here
    # would kill the run rather than degrade it. A pool holding two embedding
    # dimensions is the realistic trigger: `embedding_model` is a tuning.xlsx
    # setting, so a SharePoint edit with no deploy mixes dimensions inside the
    # 30-day window. Losing de-duplication is survivable; losing the digest is
    # not.
    try:
        candidates, dropped_duplicates = collapse_duplicates(candidates)
        if dropped_duplicates:
            _log({"step": "duplicate_collapse",
                  "dropped_duplicates": dropped_duplicates})
    except Exception as e:
        _log({"step": "duplicate_collapse_failed",
              "error": f"{type(e).__name__}: {e}"})

    try:
        candidates, dropped_near = collapse_near_duplicates(
            candidates,
            storage.load_story_embeddings([s.id for s in candidates], conn=conn),
        )
        if dropped_near:
            _log({"step": "near_duplicate_collapse", "dropped": dropped_near})
    except Exception as e:
        _log({"step": "near_duplicate_collapse_failed",
              "error": f"{type(e).__name__}: {e}"})

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
    client, rank_model = _build_ranker_client(fallback=client)
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

    decisions, llm_buckets, llm_geos, parse_fallback = parse_ranked(
        response_text, stories_by_id,
    )
    # A response that decided only a handful of stories is a degraded run, not
    # a healthy one: it is the shape max-token truncation takes (FEEDBACK #11),
    # and the undecided remainder silently falls back to the inherited geo and
    # bucket. Treat thin coverage as fallback so the Slack notice fires.
    # No `if decisions` guard: a response that decides ZERO stories is the
    # worst degraded state, not an exempt one. `{"stories": []}` and a response
    # whose story_ids match nothing both parse cleanly, so parse_fallback stays
    # False, and without this they shipped a wholly un-ranked digest — every
    # story Tier A on its inherited geo and bucket — reported as a normal run.
    coverage = len(decisions) / len(candidates) if candidates else 1.0
    partial = coverage < MIN_DECISION_COVERAGE
    if partial:
        _log({
            "step": "partial_response",
            "decided": len(decisions),
            "candidates": len(candidates),
            "coverage": round(coverage, 3),
        })
    used_fallback = bool(call_error) or parse_fallback or partial

    # Force every candidate into one of the 8 buckets (no 'Other' section). The
    # LLM picks the best fit; Track-A stories fall back to their own bucket; the
    # rare straggler lands in the default bucket rather than being dropped.
    # Geo is re-derived here too: the ranker read the story, the inherited
    # plan/publication geo only knows where we found it. See _effective_geo.
    valid = _valid_bucket_keys()
    default_bucket = _default_bucket_key()
    bucketed: list[Story] = []
    default_assigned = 0
    geo_corrected = 0
    for st in candidates:
        eff = _effective_bucket(st, llm_buckets, valid, default_bucket)
        if eff == default_bucket and llm_buckets.get(st.id) not in valid \
                and st.priority_bucket not in valid:
            default_assigned += 1
        eff_geo = _effective_geo(st, llm_geos)
        if eff_geo != st.geo:
            geo_corrected += 1
        bucketed.append(replace(st, priority_bucket=eff, geo=eff_geo))
    if default_assigned:
        _log({"step": "bucket_default_assigned", "count": default_assigned,
              "default_bucket": default_bucket})
    _log({
        "step": "geo_resolution",
        "llm_supplied": len(llm_geos),
        "candidates": len(candidates),
        "changed_from_inherited": geo_corrected,
    })

    grouped = _group_for_prompt(bucketed)
    # Uniform per-bucket selection: fixed top-5 highlights + up to per_bucket_max
    # (1-2) per bucket, no 'Other' section.
    top, by_priority, all_ranked = _select(
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
        ranked_by_bucket={k: tuple(v) for k, v in all_ranked.items()},
    )


def _empty_result(start: float) -> RankingResult:
    elapsed = round(time.monotonic() - start, 3)
    _log({"candidates_count": 0, "used_fallback": False, "latency_ms": int(elapsed * 1000)})
    return RankingResult(
        top_summary=[], by_priority={}, other=[],
        candidates_count=0, used_fallback=False,
        cost_usd=0.0, elapsed_seconds=elapsed, flat=(),
    )


def filter_by_geo(
    r: RankingResult,
    allowed: set[str],
    *,
    per_bucket_max: int = config.PER_BUCKET_MAX,
    top_summary_size: int = config.TOP_SUMMARY_SIZE,
) -> RankingResult:
    """Return a copy of `r` keeping only stories whose geo is in `allowed`.

    Used to route one ranking into two geo channels. `allowed` is e.g.
    {"India", "Global"} for the India channel. A story with geo=None (unknown)
    is treated as Global and kept for BOTH channels, matching the console's
    `_geo_tag` default — so unclassified items are never dropped.

    Selection is **re-run**, not filtered. `_select` removes a promoted story
    from its bucket body, so simply dropping a highlight left a hole nothing
    could fill: on the 3 Sept data, filtering two US stories out of the top-5
    reduced "Today's biggest stories" to a single bullet. Now the promoted
    stories are merged back into their buckets, the geo filter is applied to
    the whole set, and the top-summary and bodies are chosen again from what
    survives — so a dropped highlight is replaced by the next-best story that
    does belong in this channel.

    This mattered less when geo was a weak fetch-time proxy and most RSS
    stories were None (kept everywhere). Now that the ranker assigns a specific
    geo per story, this filter is the primary router and drops real volume.
    """
    def keep(rs: RankedStory) -> bool:
        return rs.story.geo is None or rs.story.geo in allowed

    # Select from EVERY ranked candidate, not from the geo-blind cut. Using
    # by_priority here would cap each channel at the ~21 stories that already
    # won a geo-blind slot: with a 70/20 India/US split that left the US
    # channel 6 items and 1 of 8 category sections while 24 eligible US
    # stories sat unused in the pool.
    if r.ranked_by_bucket:
        merged: dict[str, list[RankedStory]] = {
            k: list(v) for k, v in r.ranked_by_bucket.items()
        }
    else:
        # Hand-built result (tests, or a caller that didn't come through
        # _select): fall back to re-merging the promoted highlights so the
        # highlight slots at least get refilled.
        merged = {k: list(v) for k, v in r.by_priority.items()}
        default_key = _default_bucket_key()
        for rs in r.top_summary:
            key = rs.story.priority_bucket or default_key
            merged.setdefault(key, []).append(rs)

    scoped: dict[str, list[RankedStory]] = {}
    for key, items in merged.items():
        kept = [x for x in items if keep(x)]
        if kept:
            scoped[key] = sorted(kept, key=_rank_within_bucket)

    top, by_priority = _split_top_and_bodies(
        scoped, per_bucket_max=per_bucket_max, top_summary_size=top_summary_size,
    )
    flat: list[RankedStory] = list(top)
    for b in config.PRIORITY_BUCKETS:
        flat.extend(by_priority.get(b.key, []))

    return replace(
        r,
        top_summary=top,
        by_priority=by_priority,
        other=[x for x in r.other if keep(x)],
        flat=tuple(flat),
    )
