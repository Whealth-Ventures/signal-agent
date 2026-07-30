"""Sector-digest ranker — the magnitude tiering of ranker.py, re-shaped for a
bi-weekly per-sector roundup.

Differences from the daily ranker (ranker.py):
  - Candidate pool is scoped to ONE sector stream ('sector:<key>') and to a
    14-day window (not 30) — a roundup only dedupes against its own history.
  - A per-sector keyword gate runs on top of the healthcare gate, so the
    diabetes run can't leak generic health news.
  - No "≥$10M" floor and no top-5 highlight section. Stories are grouped by
    EVENT TYPE (raises → clinical → policy → product), Tier-C dropped, ordered
    within a group by tier then recency. Tier-B is kept (a $1-2M raise is news
    in a narrow sector).
  - Secondary-geo stories (the "biggest US stories" sweep for India peds/onc)
    are capped to the sector's secondary_max_stories.

Falls back to recency-order-within-group if the LLM call fails, and always
returns a result (never raises) so the digest ships.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import config
import storage
from models import Story
from query_planner import Sector, load_sector_keywords, load_sector_watchlist
from ranker import (
    Tier,
    _TIER_RANK,
    _build_ranker_client,
    _coerce_tier,
    _extract_json,
    _fallback_one_liner,
    _trim_summary,
    _RankerClient,
)
from topicality import is_healthcare, make_term_gate

# Behavioural knobs (kept here rather than tuning.xlsx for v1 — see plan).
SECTOR_DEDUP_WINDOW_DAYS = 14
SECTOR_LOOKBACK_DAYS = 14
SECTOR_PER_GROUP_MAX = 8
SECTOR_CANDIDATE_POOL = 200

# Event-type buckets, in display order. The LLM assigns each story exactly one
# key; anything unmatched falls back to the catch-all (last entry).
EVENT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("raises", "Fundraising, M&A & IPOs"),
    ("clinical", "Clinical & Regulatory"),
    ("policy", "Policy, Legal & Enforcement"),
    ("product", "Product, Partnerships & People"),
)
_EVENT_KEYS = tuple(k for k, _ in EVENT_BUCKETS)
_EVENT_ORDER = {k: i for i, (k, _) in enumerate(EVENT_BUCKETS)}
_DEFAULT_EVENT = "product"
ONE_LINER_MAX_CHARS = config.ONE_LINER_MAX_CHARS


@dataclass(frozen=True)
class SectorBullet:
    """One line in the digest. Usually one story; for a roll-up it represents a
    cluster of related stories merged into a single sentence. `story` is the
    representative item (drives the link, geo tag, and recency ordering);
    `member_story_ids` lists every story the bullet covers so all of them are
    recorded as sent for dedup. Exposes `.story` / `.one_liner` / `.tier` so the
    Slack + PDF renderers consume it exactly like the daily ranker's RankedStory."""
    story: Story
    tier: Tier
    one_liner: str
    event: str
    member_story_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectorRankingResult:
    sector_key: str
    groups: list[tuple[str, list[SectorBullet]]]  # (event display, bullets), ordered
    candidates_count: int
    used_fallback: bool
    cost_usd: float
    elapsed_seconds: float
    summary: str = ""   # 2-3 sentence prose lead (hybrid voice); "" if none
    flat: tuple[SectorBullet, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return sum(len(v) for _, v in self.groups)


# --- Prompt -------------------------------------------------------------

def _event_legend() -> str:
    return "\n".join(f"  - {k} — {d}" for k, d in EVENT_BUCKETS)


def build_sector_prompt(sector: Sector, stories: list[Story]) -> str:
    keys = ", ".join(_EVENT_KEYS)
    lines = [
        f"Compile the {sector.display} sector roundup for the last 14 days. "
        "Tier each candidate below with this rubric:",
        "",
        config.MAGNITUDE_RUBRIC,
        "",
        "IMPORTANT for a single-sector roundup: do NOT apply a funding-size "
        "floor. A small raise, a single-clinic milestone, or an early readout "
        "IS newsworthy here. Use Tier C only for off-sector, pure-opinion, or "
        "non-substantive items (Tier C is dropped).",
        "",
        "Produce a list of ITEMS. Each item becomes one bullet in the digest. "
        "Most items reference a SINGLE story. When two or more candidates "
        "describe the same event or a clearly related cluster, MERGE them into "
        "one item that lists ALL their story_ids and a single combined "
        "one-liner (see the roll-up guidance in your instructions). Do not "
        "merge unrelated items. Every candidate should appear in exactly one "
        "item unless it is Tier C.",
        "",
        "For EACH item return:",
        '  - `story_ids`: list of one or more ids from the candidates below',
        '  - `tier`: "S", "A", "B", or "C" (the most significant component\'s tier)',
        f"  - `one_liner`: the bullet sentence (aim ≤ ~{ONE_LINER_MAX_CHARS} chars; a "
        "second short sentence is allowed only for a material detail). Lead with "
        "the SPECIFIC WHAT — company, number, action, outcome. British spelling. "
        "No geo prefix (added automatically).",
        f"  - `event`: EXACTLY one of these keys — {keys}:",
        _event_legend(),
        "",
        "Also write a `summary`: 2–3 sentences of prose setting the scene for the "
        "fortnight (see your instructions). Name the standout items in flowing "
        "sentences, British spelling, no salutation.",
        "",
        "Return ONLY a JSON object (no markdown fences):",
        '{"summary": "Headspace signed several specialty-care partnerships and '
        'Otsuka won FDA approval for a new ADHD drug; on the research front, a '
        'Swedish study strengthened the GLP-1 evidence base.",'
        ' "items": [{"story_ids": ["<id>"], "tier": "S", "event": "raises", '
        '"one_liner": "Vanna Health raised $17M."},'
        ' {"story_ids": ["<id2>","<id3>"], "tier": "A", "event": "policy", '
        '"one_liner": "States froze behavioural health Medicaid enrolment — Utah, '
        'Maryland and Arizona."}]}',
        "",
        f"Candidates ({len(stories)} total):",
    ]
    for st in stories:
        geo = st.geo or "-"
        lines.append(f"[id={st.id}  geo={geo}]")
        lines.append(f"  title: {st.canonical_title}")
        lines.append(f"  summary: {_trim_summary(st.canonical_summary)}")
        lines.append(f"  url: {st.canonical_url}")
    return "\n".join(lines)


def parse_sector_ranked(
    response_text: str, stories_by_id: dict[str, Story],
) -> tuple[str, list[SectorBullet], bool]:
    """Parse the `{summary, items}` contract.

    Each item may reference one or more story_ids; the first known id is the
    representative story (link/geo/recency), and all known ids are recorded as
    members (for dedup). A story referenced by more than one item is attributed
    to the first item only. Returns (summary, bullets, used_fallback)."""
    parsed = _extract_json(response_text)
    bullets: list[SectorBullet] = []
    summary = ""
    if parsed and isinstance(parsed.get("summary"), str):
        summary = parsed["summary"].strip().replace("\n", " ")
    # Accept `items` (roll-up contract); tolerate a legacy `stories` list too.
    items = None
    if parsed and isinstance(parsed.get("items"), list):
        items = parsed["items"]
    elif parsed and isinstance(parsed.get("stories"), list):
        items = [
            {"story_ids": [e.get("story_id")], **e}
            for e in parsed["stories"] if isinstance(e, dict)
        ]
    if items is None:
        return summary, bullets, True

    used: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        raw_ids = entry.get("story_ids") or []
        if not isinstance(raw_ids, list):
            continue
        ids = [
            s for s in raw_ids
            if isinstance(s, str) and s in stories_by_id and s not in used
        ]
        if not ids:
            continue
        tier = _coerce_tier(entry.get("tier"))
        if tier is None:
            continue
        primary = stories_by_id[ids[0]]
        ol = str(entry.get("one_liner") or "").strip().replace("\n", " ")
        if not ol:
            ol = _fallback_one_liner(primary)
        ev = entry.get("event")
        event = ev.strip() if isinstance(ev, str) and ev.strip() in _EVENT_KEYS \
            else _DEFAULT_EVENT
        bullets.append(SectorBullet(
            story=primary, tier=tier, one_liner=ol[:ONE_LINER_MAX_CHARS * 2],
            event=event, member_story_ids=tuple(ids),
        ))
        used.update(ids)
    return summary, bullets, False


# --- Selection ----------------------------------------------------------

def _within_lookback(st: Story, *, now: datetime, days: int) -> bool:
    cutoff = now - timedelta(days=days)
    return st.published_at >= cutoff


def _sort_key(b: SectorBullet):
    """Universal ordering: importance first (tier), then most-recent."""
    return (_TIER_RANK[b.tier], -b.story.published_at.timestamp())


def _select_groups(
    bullets: list[SectorBullet], sector: Sector,
) -> list[tuple[str, list[SectorBullet]]]:
    """Cap secondary geo, drop Tier-C, enforce the total budget, then group by
    event type, ordering + capping within each group."""
    # 1. Drop Tier-C.
    bullets = [b for b in bullets if b.tier != "C"]

    # 2. Cap secondary-geo bullets to the sector's allowance (keep the strongest).
    if sector.secondary_geo and sector.secondary_max_stories >= 0:
        sec = sorted(
            [b for b in bullets if b.story.geo == sector.secondary_geo],
            key=_sort_key,
        )
        keep_sec = {id(b) for b in sec[: sector.secondary_max_stories]}
        bullets = [
            b for b in bullets
            if b.story.geo != sector.secondary_geo or id(b) in keep_sec
        ]

    # 3. Global soft cap: if over target, keep the strongest (tier, then recency)
    #    so a slow fortnight isn't padded and a busy one isn't huge.
    target = sector.target_story_count or 15
    if len(bullets) > target:
        bullets = sorted(bullets, key=_sort_key)[:target]

    # 4. Group by event type in display order; sort within group; per-group cap.
    by_event: dict[str, list[SectorBullet]] = {k: [] for k in _EVENT_KEYS}
    for b in bullets:
        by_event.get(b.event, by_event[_DEFAULT_EVENT]).append(b)
    out: list[tuple[str, list[SectorBullet]]] = []
    for key, display in EVENT_BUCKETS:
        items = sorted(by_event.get(key, []), key=_sort_key)
        if items:
            out.append((display, items[:SECTOR_PER_GROUP_MAX]))
    return out


# --- Orchestrator -------------------------------------------------------

def rank_sector(
    sector: Sector,
    *,
    conn: sqlite3.Connection | None = None,
    client: _RankerClient | None = None,
    now: datetime | None = None,
) -> SectorRankingResult:
    start = time.monotonic()
    now = now or datetime.now(timezone.utc)
    stream = f"sector:{sector.key}"

    sent_urls = storage.recently_sent_urls(
        within_days=SECTOR_DEDUP_WINDOW_DAYS, stream=stream, conn=conn,
    )
    pool = storage.list_stories(
        min_score=0.0, limit=SECTOR_CANDIDATE_POOL,
        exclude_urls=sent_urls, order_by_recency=True, stream=stream, conn=conn,
    )

    # Gates: healthcare + on-sector (keywords + watchlist names) + 14-day window.
    terms = [kw for kw, _ in load_sector_keywords().get(sector.key, [])]
    terms += load_sector_watchlist().get(sector.key, [])
    on_sector = make_term_gate(terms)
    candidates = [
        s for s in pool
        if _within_lookback(s, now=now, days=SECTOR_LOOKBACK_DAYS)
        and is_healthcare(f"{s.canonical_title} {s.canonical_summary or ''}")
        and on_sector(f"{s.canonical_title} {s.canonical_summary or ''}")
    ]
    if not candidates:
        return SectorRankingResult(
            sector_key=sector.key, groups=[], candidates_count=0,
            used_fallback=False, cost_usd=0.0,
            elapsed_seconds=round(time.monotonic() - start, 3), flat=(),
        )

    stories_by_id = {s.id: s for s in candidates}
    prompt = build_sector_prompt(sector, candidates)

    call_error: str | None = None
    response_text = ""
    cost = 0.0
    if client is None:
        client, rank_model = _build_ranker_client()
    else:
        rank_model = config.PERPLEXITY_MODEL_RANK
    try:
        resp = client.complete(
            prompt, model=rank_model, query_id=f"sector_rank_{sector.key}",
            system=config.SECTOR_RANKER_SYSTEM_PROMPT,
            timeout=config.HTTP_TIMEOUT_RANK_S,
        )
        response_text = resp.text
        cost = resp.estimated_cost_usd
    except Exception as e:  # degrade to recency-order, still ship
        call_error = f"{type(e).__name__}: {e}"

    summary, bullets, parse_fallback = parse_sector_ranked(response_text, stories_by_id)
    used_fallback = bool(call_error) or parse_fallback
    if not bullets:
        # Fallback: one bullet per candidate (Tier A, title as one-liner),
        # newest-first via _select_groups. Digest still ships.
        bullets = [
            SectorBullet(
                story=s, tier="A", one_liner=_fallback_one_liner(s),
                event=_DEFAULT_EVENT, member_story_ids=(s.id,),
            )
            for s in candidates
        ]

    groups = _select_groups(bullets, sector)
    flat: list[SectorBullet] = [b for _, items in groups for b in items]

    _log({
        "sector": sector.key,
        "candidates_count": len(candidates),
        "total_selected": len(flat),
        "groups": {d: len(v) for d, v in groups},
        "used_fallback": used_fallback,
        "cost_usd": cost,
        "call_error": call_error,
    })

    return SectorRankingResult(
        sector_key=sector.key,
        groups=groups,
        candidates_count=len(candidates),
        used_fallback=used_fallback,
        cost_usd=cost,
        elapsed_seconds=round(time.monotonic() - start, 3),
        summary=summary,
        flat=tuple(flat),
    )


# --- Logging ------------------------------------------------------------

def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    path = config.LOGS_DIR / f"sector_ranker_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
