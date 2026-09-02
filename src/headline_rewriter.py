"""Post-selection headline rewrite: read the article, fix the one-liner.

The ranker writes each story's one-liner from the fetched title + a ≤500-char
summary snippet — when the source headline is vague, the one-liner is too.
This step runs AFTER ranking + geo filtering, so it only touches the ~15–25
stories that actually ship: it GETs each winner's article, extracts a body
excerpt, and makes ONE further LLM call (same vendor selection as the ranker —
Claude when configured, else Perplexity) to rewrite the one-liners from the
article text.

Fail-soft at every level: a story whose fetch fails keeps its ranker one-liner;
if the LLM call fails (or returns garbage), the whole ranking is returned
unchanged. The digest always ships.

Audit trail: data/logs/headlines_<date>.jsonl — one line per story with the
old and new headline, plus one summary line per run.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config
from ranker import (
    RankedStory,
    RankingResult,
    _build_ranker_client,
    _extract_json,
)

FETCH_CONCURRENCY = 10
FETCH_TIMEOUT_S = 12.0
EXCERPT_CHARS = 2000          # per-article excerpt handed to the LLM
# Same tunable the ranker's one-liners obey, so a tuning.xlsx change to the
# headline length reaches the whole digest and not just part of it.
MAX_HEADLINE_CHARS = config.ONE_LINER_MAX_CHARS
_UA = "SignalAgent/0.1"

_TAG_BLOCK_RE = re.compile(
    r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_P_RE = re.compile(r"<p[ >].*?</p>|<p>.*?</p>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"headlines_{_today_str()}.jsonl"


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_excerpt(raw_html: str, *, limit: int = EXCERPT_CHARS) -> str:
    """Article-body excerpt from raw HTML.

    Prefers the text inside <p> tags (nav chrome lives in lists and links, body
    copy in paragraphs); falls back to strip-all-tags when a page has no
    paragraphs. ponytail: regex extraction, add a real parser only if this
    misses on sources that matter.
    """
    if not raw_html:
        return ""
    cleaned = _TAG_BLOCK_RE.sub(" ", raw_html)
    paras = _P_RE.findall(cleaned)
    text = " ".join(paras) if paras else cleaned
    text = _ANY_TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return _WS_RE.sub(" ", text).strip()[:limit]


async def _fetch_one(url: str, http: httpx.AsyncClient, sem: asyncio.Semaphore) -> str:
    async with sem:
        try:
            r = await http.get(url)
            if r.status_code >= 400:
                return ""
            return extract_excerpt(r.text)
        except Exception:
            # Deliberately broad: httpx.InvalidURL is NOT an httpx.HTTPError
            # (its MRO is InvalidURL → Exception), and a digest URL is only as
            # clean as the source that published it. One bad URL must cost its
            # own excerpt, never the whole run's.
            return ""


async def _fetch_excerpts(urls: list[str]) -> dict[str, str]:
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as http:
        # return_exceptions so a straggler that escapes _fetch_one's guard
        # degrades to one empty excerpt instead of cancelling its siblings.
        results = await asyncio.gather(
            *(_fetch_one(u, http, sem) for u in urls), return_exceptions=True,
        )
    return {
        u: (r if isinstance(r, str) else "")
        for u, r in zip(urls, results)
    }


def _clean_headline(v: object) -> str | None:
    """Normalise one LLM headline, or None if it's unusable.

    An over-length headline is trimmed at a word boundary rather than dropped:
    a sharp headline a few chars long beats the vague ranker one-liner this
    step exists to replace. Matches how the ranker treats its own one-liners.
    """
    if not isinstance(v, str):
        return None
    s = _WS_RE.sub(" ", v).strip().strip('"')
    if not s:
        return None
    if len(s) > MAX_HEADLINE_CHARS:
        cut = s[:MAX_HEADLINE_CHARS].rstrip()
        if " " in cut:
            cut = cut[: cut.rfind(" ")].rstrip()
        s = cut.rstrip(",;:-") or None
    return s


def _request_rewrites(
    items: list[dict], client, model: str,
) -> tuple[dict[str, str], float]:
    """One LLM call: [{id,title,current,excerpt}] → ({id: new headline}, cost).

    A parse failure logs the response body. Without that, an unparseable
    response and "the LLM kept every headline" both look like `rewritten: 0`
    and there is no way to tell them apart after the fact — the same blind spot
    that hid the ranker's max-tokens truncation for five days in Aug 2026.
    """
    resp = client.complete(
        json.dumps({"stories": items}, ensure_ascii=False),
        model=model,
        query_id="headline_rewrite",
        system=config.HEADLINE_SYSTEM_PROMPT,
        timeout=float(config.HTTP_TIMEOUT_RANK_S),
    )
    cost = float(getattr(resp, "estimated_cost_usd", 0.0) or 0.0)
    parsed = _extract_json(resp.text)
    if not parsed or not isinstance(parsed.get("headlines"), dict):
        _log({
            "event": "parse_failed",
            "model": getattr(resp, "model", model),
            "response_len": len(resp.text or ""),
            "response_text": (resp.text or "")[:2000],
        })
        return {}, cost
    out: dict[str, str] = {}
    for sid, v in parsed["headlines"].items():
        s = _clean_headline(v)
        if s:
            out[str(sid)] = s
    return out, cost


def _apply(items: list[RankedStory], new: dict[str, str]) -> list[RankedStory]:
    return [
        replace(r, one_liner=new[r.story.id]) if r.story.id in new else r
        for r in items
    ]


def rewrite_headlines(
    ranking: RankingResult, *, fallback_client=None,
) -> RankingResult:
    """Return a copy of `ranking` with article-grounded one-liners.

    `fallback_client` is the caller's Perplexity fetch client, passed through
    to _build_ranker_client for the same reason the ranker does: on the
    Perplexity path the per-(date, geo) budget counter must stay shared.
    Any failure returns `ranking` unchanged.
    """
    winners = list(ranking.flat)
    if not winners:
        return ranking
    try:
        # Build the client FIRST: when the configured vendor can't serve this
        # step, the documented no-op should cost nothing, not 25 outbound
        # requests and ~12s for a result we discard.
        client, model = _build_ranker_client(fallback_client)

        urls = list({r.story.canonical_url for r in winners if r.story.canonical_url})
        excerpts = asyncio.run(_fetch_excerpts(urls))
        fetched = sum(1 for v in excerpts.values() if v)

        items = [
            {
                "id": r.story.id,
                "title": r.story.canonical_title,
                "current": r.one_liner,
                "excerpt": excerpts.get(r.story.canonical_url, ""),
            }
            for r in winners
        ]
        # Nothing fetched → the LLM would only see what the ranker already saw.
        if fetched == 0:
            _log({"event": "skip_no_excerpts", "stories": len(winners)})
            return ranking

        new, cost = _request_rewrites(items, client, model)

        for r in winners:
            if r.story.id in new and new[r.story.id] != r.one_liner:
                _log({
                    "event": "rewrite",
                    "story_id": r.story.id,
                    "url": r.story.canonical_url,
                    "fetched": bool(excerpts.get(r.story.canonical_url)),
                    "old": r.one_liner,
                    "new": new[r.story.id],
                })
        _log({
            "event": "run_done",
            "stories": len(winners),
            "excerpts_fetched": fetched,
            "cost_usd": cost,
            "rewritten": sum(
                1 for r in winners
                if r.story.id in new and new[r.story.id] != r.one_liner
            ),
        })
        return replace(
            ranking,
            top_summary=_apply(ranking.top_summary, new),
            by_priority={
                k: _apply(v, new) for k, v in ranking.by_priority.items()
            },
            other=_apply(ranking.other, new),
            flat=tuple(_apply(winners, new)),
            # Roll this call into the run's cost so the pipeline summary isn't
            # short by one ranker-class call.
            cost_usd=ranking.cost_usd + cost,
        )
    except Exception as e:  # fail-soft: never block the digest on a rewrite
        try:
            _log({"event": "error", "error": f"{type(e).__name__}: {e}"})
        except Exception:
            # run_pipeline is try/finally with no except: an unwritable
            # LOGS_DIR here would otherwise abort the run before the Slack
            # post, which is the one hole in "this step can never block the
            # digest".
            pass
        return ranking
