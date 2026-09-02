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
MAX_HEADLINE_CHARS = 90       # hard cap; longer LLM output is discarded
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
        except httpx.HTTPError:
            return ""


async def _fetch_excerpts(urls: list[str]) -> dict[str, str]:
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as http:
        results = await asyncio.gather(*(_fetch_one(u, http, sem) for u in urls))
    return dict(zip(urls, results))


def _clean_headline(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = _WS_RE.sub(" ", v).strip().strip('"')
    if not s or len(s) > MAX_HEADLINE_CHARS:
        return None
    return s


def _request_rewrites(
    items: list[dict], client, model: str,
) -> dict[str, str]:
    """One LLM call: [{id,title,current,excerpt}] → {id: new headline}."""
    resp = client.complete(
        json.dumps({"stories": items}, ensure_ascii=False),
        model=model,
        query_id="headline_rewrite",
        system=config.HEADLINE_SYSTEM_PROMPT,
        timeout=float(config.HTTP_TIMEOUT_RANK_S),
    )
    parsed = _extract_json(resp.text)
    if not parsed or not isinstance(parsed.get("headlines"), dict):
        return {}
    out: dict[str, str] = {}
    for sid, v in parsed["headlines"].items():
        s = _clean_headline(v)
        if s:
            out[str(sid)] = s
    return out


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

        client, model = _build_ranker_client(fallback_client)
        new = _request_rewrites(items, client, model)

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
        )
    except Exception as e:  # fail-soft: never block the digest on a rewrite
        _log({"event": "error", "error": f"{type(e).__name__}: {e}"})
        return ranking
