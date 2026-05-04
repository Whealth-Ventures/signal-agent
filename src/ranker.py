"""Layer 4: editorial ranker.

Pulls the top-N candidate stories from storage (already scored + deduped +
recent-URL-filtered by scorer.py), sends them to Perplexity sonar-reasoning,
parses the JSON ranking, returns an ordered list of 5. Doesn't persist —
main.py wraps this with storage.create_digest + add_story_to_digest.

If the LLM response can't be parsed, falls back to score-order so the digest
still ships. used_fallback=True surfaces that to the caller.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import config
import storage
from models import Story
from perplexity_client import ChatResponse, PerplexityClient

CANDIDATE_POOL_SIZE = 25
SUMMARY_MAX_CHARS_IN_PROMPT = 200
REASONING_MAX_CHARS = 280

_SYSTEM_PROMPT = (
    "You are an editor curating a daily healthcare news digest for an Indian and "
    "US healthcare VC firm. You will not search the web for this task — reason only "
    "over the candidate stories provided. Output JSON only, no preamble."
)


class _RankerClient(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        model: str = ...,
        recency: str | None = ...,
        query_id: str = ...,
        system: str | None = ...,
    ) -> ChatResponse: ...


@dataclass(frozen=True)
class RankedStory:
    story: Story
    rank: int
    reasoning: str


@dataclass(frozen=True)
class RankingResult:
    ranked: list[RankedStory]
    candidates_count: int
    used_fallback: bool
    cost_usd: float
    elapsed_seconds: float


# --- Prompt building ----------------------------------------------------

def _trim_summary(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) > SUMMARY_MAX_CHARS_IN_PROMPT:
        s = s[:SUMMARY_MAX_CHARS_IN_PROMPT - 1].rstrip() + "…"
    return s


def build_prompt(candidates: list[Story], top_n: int) -> str:
    lines = [
        f"Pick the TOP {top_n} stories that should appear in today's digest. Consider:",
        "- Genuine newsworthiness (funding rounds, launches, regulatory, M&A, policy)",
        "- Avoid near-duplicates",
        "- Mix India + US + cross-cutting themes if possible",
        "- Skip listicles, hot takes, and opinion pieces",
        "",
        "Return ONLY a JSON object with this exact structure (no markdown fences):",
        "{",
        '  "ranked": [',
        '    {"story_id": "<id from candidates below>", "rank": 1,',
        '     "reasoning": "One sentence explaining why this matters."},',
        "    ...",
        "  ]",
        "}",
        "",
        f"Candidates ({len(candidates)} stories, sorted by pre-computed relevance score):",
    ]
    for i, st in enumerate(candidates, start=1):
        lines.extend([
            f"[{i}] id={st.id}  score={st.relevance_score:.3f}",
            f"    {st.canonical_title}",
            f"    Summary: {_trim_summary(st.canonical_summary)}",
            f"    URL: {st.canonical_url}",
        ])
    return "\n".join(lines)


# --- Response parsing ---------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    s = text.strip()
    fence_match = _FENCE_RE.match(s)
    if fence_match:
        s = fence_match.group(1).strip()
    # Try direct parse first
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Heuristic: slice from first { to last }
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_ranked(
    response_text: str,
    candidates_by_id: dict[str, Story],
    top_n: int,
) -> tuple[list[RankedStory], bool]:
    """Returns (ranked_list, used_fallback). Fills empty slots from score order."""
    parsed = _extract_json(response_text)
    chosen_ids: list[str] = []
    reasonings: dict[str, str] = {}
    fallback = False

    if parsed and isinstance(parsed.get("ranked"), list):
        for entry in parsed["ranked"]:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("story_id")
            if not isinstance(sid, str) or sid not in candidates_by_id:
                continue
            if sid in chosen_ids:
                continue
            chosen_ids.append(sid)
            reasoning = str(entry.get("reasoning") or "").strip()
            reasonings[sid] = reasoning[:REASONING_MAX_CHARS]
            if len(chosen_ids) >= top_n:
                break
    else:
        fallback = True

    # Fill remaining slots from score-ordered fallback
    if len(chosen_ids) < top_n:
        if not fallback and len(chosen_ids) == 0:
            fallback = True
        ordered = sorted(
            candidates_by_id.values(),
            key=lambda s: -s.relevance_score,
        )
        for st in ordered:
            if st.id in chosen_ids:
                continue
            chosen_ids.append(st.id)
            reasonings.setdefault(
                st.id,
                f"Fallback: relevance score {st.relevance_score:.2f}.",
            )
            if len(chosen_ids) >= top_n:
                break
        if not fallback and any(
            r.startswith("Fallback:") for r in reasonings.values()
        ):
            # We had to fill slots → mark as fallback for transparency
            fallback = True

    ranked = [
        RankedStory(
            story=candidates_by_id[sid],
            rank=i + 1,
            reasoning=reasonings.get(sid, ""),
        )
        for i, sid in enumerate(chosen_ids[:top_n])
    ]
    return ranked, fallback


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

def rank_stories(
    *,
    top_n: int = config.DIGEST_TOP_N,
    candidate_pool_size: int = CANDIDATE_POOL_SIZE,
    conn: sqlite3.Connection | None = None,
    client: _RankerClient | None = None,
) -> RankingResult:
    start = time.monotonic()
    candidates = storage.list_stories(limit=candidate_pool_size, conn=conn)

    if not candidates:
        result = RankingResult(
            ranked=[], candidates_count=0, used_fallback=False,
            cost_usd=0.0, elapsed_seconds=round(time.monotonic() - start, 3),
        )
        _log({
            "candidates_count": 0, "top_n": top_n, "used_fallback": False,
            "model": None, "cost_usd": 0.0,
            "latency_ms": int(result.elapsed_seconds * 1000),
            "ranked_ids": [],
        })
        return result

    # Short-circuit: pool is small enough that we don't need an LLM.
    if len(candidates) <= top_n:
        ranked = [
            RankedStory(
                story=st,
                rank=i + 1,
                reasoning=f"Auto-selected (≤{top_n} candidates).",
            )
            for i, st in enumerate(
                sorted(candidates, key=lambda s: -s.relevance_score)
            )
        ]
        elapsed = round(time.monotonic() - start, 3)
        _log({
            "candidates_count": len(candidates), "top_n": top_n,
            "used_fallback": False, "model": None, "cost_usd": 0.0,
            "latency_ms": int(elapsed * 1000),
            "ranked_ids": [r.story.id for r in ranked],
        })
        return RankingResult(
            ranked=ranked, candidates_count=len(candidates),
            used_fallback=False, cost_usd=0.0, elapsed_seconds=elapsed,
        )

    # Full LLM ranking path
    if client is None:
        client = PerplexityClient()
    prompt = build_prompt(candidates, top_n)
    response = client.complete(
        prompt,
        model=config.PERPLEXITY_MODEL_RANK,
        query_id="rank",
        system=_SYSTEM_PROMPT,
    )

    by_id = {st.id: st for st in candidates}
    ranked, used_fallback = parse_ranked(response.text, by_id, top_n)
    elapsed = round(time.monotonic() - start, 3)

    _log({
        "candidates_count": len(candidates), "top_n": top_n,
        "used_fallback": used_fallback,
        "model": response.model, "cost_usd": response.estimated_cost_usd,
        "latency_ms": int(elapsed * 1000),
        "ranked_ids": [r.story.id for r in ranked],
        "response_text": response.text[:2000],
    })

    return RankingResult(
        ranked=ranked,
        candidates_count=len(candidates),
        used_fallback=used_fallback,
        cost_usd=response.estimated_cost_usd,
        elapsed_seconds=elapsed,
    )
