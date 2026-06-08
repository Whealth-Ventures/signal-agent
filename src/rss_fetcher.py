"""Layer 2 fetcher: pulls RSS/Atom feeds for the newsletters listed in voices.xlsx.

Discovers each newsletter's feed URL via heuristics (/feed, /rss, /feed.xml,
/atom.xml) and falls back to <link rel="alternate"> in the site HTML. Filters
entries to the last N hours (default 24) and emits Signal objects.

Voices with an `rss_url` (column J of the Top Voices tabs) ARE chased here via
`fetch_all_voice_feeds()` — many publish on a Substack / blog / podcast with a
real feed, which is far more reliable than asking Perplexity to find what they
posted. Voices without a feed (most LinkedIn/X-only voices) are still covered by
the tier-1 voice-anchored Perplexity queries (module 2) instead. Note: LinkedIn
itself has no usable feed, so LinkedIn-only voices can't be chased directly.

In production `fetch_all_newsletters()` runs newsletter discovery+fetch
concurrently via httpx.AsyncClient under an asyncio.Semaphore. Tests that
inject a sync httpx.Client get the original serial path so MockTransport
fixtures keep working.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

import config
from models import Signal
from query_planner import Newsletter, load_newsletters, load_voices

USER_AGENT = "SignalAgent/0.1 (healthcare news digest)"
SUMMARY_MAX_CHARS = 500

# Cap on concurrent newsletter HTTP requests in the async path. ~60 newsletters
# × up to 5 GETs each (4 heuristic paths + HTML fallback) → don't want to
# saturate the upstream sites.
NEWSLETTER_CONCURRENCY = 10

_FEED_PATH_HEURISTICS = ("/feed", "/rss", "/feed.xml", "/atom.xml")
_RSS_LINK_TYPES = ("application/rss+xml", "application/atom+xml")

_DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml, "
        "text/xml, */*"
    ),
}


# --- Helpers ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"rss_{_today_str()}.jsonl"


def _struct_time_to_utc(ts) -> datetime:
    """feedparser returns time.struct_time in UTC; calendar.timegm is the inverse."""
    return datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)


def _entry_pub_dt(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        ts = entry.get(attr) if hasattr(entry, "get") else getattr(entry, attr, None)
        if ts:
            try:
                return _struct_time_to_utc(ts)
            except (OverflowError, ValueError):
                continue
    return None


def _is_parseable_feed(body: str) -> bool:
    if not body:
        return False
    parsed = feedparser.parse(body)
    if parsed.entries:
        return True
    # Some healthy feeds are momentarily empty; accept if feed-level title is set
    # AND feedparser found no parse errors.
    feed_meta = getattr(parsed, "feed", None) or {}
    return bool(feed_meta.get("title")) and parsed.bozo == 0


def _make_default_http() -> httpx.Client:
    return httpx.Client(
        timeout=config.HTTP_TIMEOUT_S,
        headers=_DEFAULT_HEADERS,
        follow_redirects=True,
    )


def _make_default_async_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT_S,
        headers=_DEFAULT_HEADERS,
        follow_redirects=True,
    )


def _safe_get(http: httpx.Client, url: str) -> tuple[str, int, str | None]:
    """Return (body, status, error_message). status=0 means transport-level failure."""
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        return "", 0, f"{type(e).__name__}: {e}"
    return r.text, r.status_code, None


async def _safe_get_async(http: httpx.AsyncClient, url: str) -> tuple[str, int, str | None]:
    try:
        r = await http.get(url)
    except httpx.HTTPError as e:
        return "", 0, f"{type(e).__name__}: {e}"
    return r.text, r.status_code, None


def _heuristic_method_label(path: str) -> str:
    return "heuristic_" + path.lstrip("/").replace(".", "_")


def _parse_html_for_feed_link(html: str, site_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        link_type = (link.get("type") or "").lower()
        if any(t in link_type for t in _RSS_LINK_TYPES):
            href = link.get("href")
            if href:
                return urljoin(site_url, href)
    return None


# --- Public API ---------------------------------------------------------

@dataclass(frozen=True)
class FeedFetchInfo:
    feed_url: str
    discovery: str             # one of "heuristic_<path>", "html_link", "failed"
    status: int
    items_total: int
    items_within_window: int
    latency_ms: int
    error: str | None


def discover_feed_url(site_url: str, *, http: httpx.Client) -> tuple[str | None, str]:
    """Return (feed_url, discovery_method).

    Tries common feed paths first, then HTML <link rel="alternate"> discovery.
    Returns (None, "failed") if nothing parses.
    """
    url, method, _body = _discover_with_body(site_url, http=http)
    return url, method


def _discover_with_body(
    site_url: str, *, http: httpx.Client,
) -> tuple[str | None, str, str | None]:
    """Same as discover_feed_url but also returns the feed body when a
    heuristic path already fetched a parseable feed — so the caller can
    skip re-downloading it."""
    base = site_url.rstrip("/")
    for path in _FEED_PATH_HEURISTICS:
        candidate = base + path
        body, status, _err = _safe_get(http, candidate)
        if status == 200 and _is_parseable_feed(body):
            return candidate, _heuristic_method_label(path), body

    body, status, _err = _safe_get(http, site_url)
    if status == 200 and body:
        href = _parse_html_for_feed_link(body, site_url)
        if href:
            return href, "html_link", None
    return None, "failed", None


async def _discover_with_body_async(
    site_url: str, *, http: httpx.AsyncClient,
) -> tuple[str | None, str, str | None]:
    base = site_url.rstrip("/")
    for path in _FEED_PATH_HEURISTICS:
        candidate = base + path
        body, status, _err = await _safe_get_async(http, candidate)
        if status == 200 and _is_parseable_feed(body):
            return candidate, _heuristic_method_label(path), body

    body, status, _err = await _safe_get_async(http, site_url)
    if status == 200 and body:
        href = _parse_html_for_feed_link(body, site_url)
        if href:
            return href, "html_link", None
    return None, "failed", None


def _parse_feed_body(
    body: str, *, source_name: str, since: datetime,
) -> tuple[list[Signal], int]:
    """Parse a feed body into Signals filtered to `since`. Returns
    (signals, items_total). Pure function — no HTTP."""
    parsed = feedparser.parse(body)
    items_total = len(parsed.entries)
    signals: list[Signal] = []
    for entry in parsed.entries:
        pub = _entry_pub_dt(entry)
        if pub is None:        # skip no-pub-date entries (project policy)
            continue
        if pub < since:
            continue
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = (entry.get("summary") or "").strip()[:SUMMARY_MAX_CHARS]
        signals.append(Signal(
            source=source_name,
            source_type="rss",
            title=title,
            url=link,
            published_at=pub,
            summary=summary,
            raw=dict(entry) if hasattr(entry, "keys") else {},
        ))
    return signals, items_total


def fetch_feed(
    feed_url: str,
    *,
    source_name: str,
    since: datetime,
    http: httpx.Client,
) -> tuple[list[Signal], FeedFetchInfo]:
    t0 = time.monotonic()
    body, status, transport_err = _safe_get(http, feed_url)
    if transport_err or status != 200:
        return [], FeedFetchInfo(
            feed_url=feed_url, discovery="", status=status, items_total=0,
            items_within_window=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=transport_err or f"HTTP {status}",
        )
    signals, items_total = _parse_feed_body(body, source_name=source_name, since=since)
    return signals, FeedFetchInfo(
        feed_url=feed_url, discovery="", status=status,
        items_total=items_total, items_within_window=len(signals),
        latency_ms=int((time.monotonic() - t0) * 1000),
        error=None,
    )


async def _fetch_feed_async(
    feed_url: str,
    *,
    source_name: str,
    since: datetime,
    http: httpx.AsyncClient,
    body: str | None = None,
) -> tuple[list[Signal], FeedFetchInfo]:
    """Async variant. If `body` is provided (already downloaded during
    discovery) we parse it directly instead of re-fetching."""
    t0 = time.monotonic()
    if body is None:
        body_str, status, transport_err = await _safe_get_async(http, feed_url)
        if transport_err or status != 200:
            return [], FeedFetchInfo(
                feed_url=feed_url, discovery="", status=status, items_total=0,
                items_within_window=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=transport_err or f"HTTP {status}",
            )
    else:
        body_str = body
        status = 200
    signals, items_total = _parse_feed_body(body_str, source_name=source_name, since=since)
    return signals, FeedFetchInfo(
        feed_url=feed_url, discovery="", status=status,
        items_total=items_total, items_within_window=len(signals),
        latency_ms=int((time.monotonic() - t0) * 1000),
        error=None,
    )


def fetch_all_newsletters(
    *,
    since_hours: int = 24,
    http: httpx.Client | None = None,
    newsletters: Iterable[Newsletter] | None = None,
) -> list[Signal]:
    """Fetch all newsletters. When `http` is passed (test path), uses the
    serial sync code. When it isn't, runs the concurrent async path."""
    if http is not None:
        return _fetch_all_newsletters_sync(
            since_hours=since_hours, http=http, newsletters=newsletters,
        )
    return asyncio.run(_fetch_all_newsletters_async(
        since_hours=since_hours, newsletters=newsletters,
    ))


def _voices_as_feeds() -> list[Newsletter]:
    """Voices that have an rss_url, wrapped as Newsletter objects so they reuse
    the same discovery+parse machinery. `type_='voice'` tags their origin."""
    out: list[Newsletter] = []
    for v in load_voices():
        url = (v.rss_url or "").strip()
        if not url:
            continue
        out.append(Newsletter(
            name=v.name, geography=v.geography, type_="voice",
            author=v.name, description=v.role, reach=v.reach_indicator, url=url,
        ))
    return out


def fetch_all_voice_feeds(
    *,
    since_hours: int = 24,
    http: httpx.Client | None = None,
) -> list[Signal]:
    """Fetch the feeds of voices that have an rss_url. Returns [] when no voice
    has a feed configured, so this is a safe no-op until the column is filled."""
    feeds = _voices_as_feeds()
    if not feeds:
        return []
    return fetch_all_newsletters(
        since_hours=since_hours, http=http, newsletters=feeds,
    )


def _fetch_all_newsletters_sync(
    *,
    since_hours: int,
    http: httpx.Client,
    newsletters: Iterable[Newsletter] | None,
) -> list[Signal]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    nls = list(newsletters) if newsletters is not None else load_newsletters()

    all_signals: list[Signal] = []
    for nl in nls:
        site = (nl.url or "").strip()
        if not site:
            continue

        t0 = time.monotonic()
        feed_url, discovery, body = _discover_with_body(site, http=http)
        if not feed_url:
            _log_record({
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "source": nl.name,
                "site_url": site,
                "feed_url": None,
                "discovery": "failed",
                "status": 0,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "items_total": 0,
                "items_within_window": 0,
                "error": "no feed discovered",
            })
            continue

        if body is not None:
            # Heuristic GET already returned a parseable feed — skip re-fetching.
            signals, items_total = _parse_feed_body(
                body, source_name=nl.name, since=since,
            )
            info = FeedFetchInfo(
                feed_url=feed_url, discovery=discovery, status=200,
                items_total=items_total, items_within_window=len(signals),
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=None,
            )
        else:
            signals, info = fetch_feed(
                feed_url, source_name=nl.name, since=since, http=http,
            )
        all_signals.extend(signals)
        _log_record({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "source": nl.name,
            "site_url": site,
            "feed_url": feed_url,
            "discovery": discovery,
            "status": info.status,
            "latency_ms": info.latency_ms,
            "items_total": info.items_total,
            "items_within_window": info.items_within_window,
            "error": info.error,
        })

    all_signals.sort(
        key=lambda s: (s.published_at, s.source, s.title),
        reverse=True,
    )
    return all_signals


async def _fetch_all_newsletters_async(
    *,
    since_hours: int,
    newsletters: Iterable[Newsletter] | None,
) -> list[Signal]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    nls = list(newsletters) if newsletters is not None else load_newsletters()
    sem = asyncio.Semaphore(NEWSLETTER_CONCURRENCY)

    async with _make_default_async_http() as http:
        async def one(nl: Newsletter) -> list[Signal]:
            site = (nl.url or "").strip()
            if not site:
                return []
            async with sem:
                t0 = time.monotonic()
                feed_url, discovery, body = await _discover_with_body_async(
                    site, http=http,
                )
                if not feed_url:
                    _log_record({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "source": nl.name,
                        "site_url": site,
                        "feed_url": None,
                        "discovery": "failed",
                        "status": 0,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "items_total": 0,
                        "items_within_window": 0,
                        "error": "no feed discovered",
                    })
                    return []
                signals, info = await _fetch_feed_async(
                    feed_url, source_name=nl.name, since=since,
                    http=http, body=body,
                )
                _log_record({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "source": nl.name,
                    "site_url": site,
                    "feed_url": feed_url,
                    "discovery": discovery,
                    "status": info.status,
                    "latency_ms": info.latency_ms,
                    "items_total": info.items_total,
                    "items_within_window": info.items_within_window,
                    "error": info.error,
                })
                return signals

        results = await asyncio.gather(*(one(nl) for nl in nls))

    all_signals: list[Signal] = [s for batch in results for s in batch]
    all_signals.sort(
        key=lambda s: (s.published_at, s.source, s.title),
        reverse=True,
    )
    return all_signals


# --- Logging ------------------------------------------------------------

def _log_record(rec: dict) -> None:
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
