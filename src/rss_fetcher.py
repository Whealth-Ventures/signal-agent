"""Layer 2 fetcher: pulls RSS/Atom feeds for the newsletters listed in voices.xlsx.

Discovers each newsletter's feed URL via heuristics (/feed, /rss, /feed.xml,
/atom.xml) and falls back to <link rel="alternate"> in the site HTML. Filters
entries to the last N hours (default 24) and emits Signal objects.

Voices are NOT chased here — most tier-1 voices publish on LinkedIn/X with no
RSS, and the spreadsheet has no rss_url column. They're covered by tier-1
voice-anchored Perplexity queries (module 2) instead.
"""
from __future__ import annotations

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
from query_planner import Newsletter, load_newsletters

USER_AGENT = "SignalAgent/0.1 (healthcare news digest)"
SUMMARY_MAX_CHARS = 500

_FEED_PATH_HEURISTICS = ("/feed", "/rss", "/feed.xml", "/atom.xml")
_RSS_LINK_TYPES = ("application/rss+xml", "application/atom+xml")


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
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
        follow_redirects=True,
    )


def _safe_get(http: httpx.Client, url: str) -> tuple[str, int, str | None]:
    """Return (body, status, error_message). status=0 means transport-level failure."""
    try:
        r = http.get(url)
    except httpx.HTTPError as e:
        return "", 0, f"{type(e).__name__}: {e}"
    return r.text, r.status_code, None


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
    base = site_url.rstrip("/")
    for path in _FEED_PATH_HEURISTICS:
        candidate = base + path
        body, status, _err = _safe_get(http, candidate)
        if status == 200 and _is_parseable_feed(body):
            method = "heuristic_" + path.lstrip("/").replace(".", "_")
            return candidate, method

    body, status, _err = _safe_get(http, site_url)
    if status == 200 and body:
        soup = BeautifulSoup(body, "html.parser")
        for link in soup.find_all("link", attrs={"rel": "alternate"}):
            link_type = (link.get("type") or "").lower()
            if any(t in link_type for t in _RSS_LINK_TYPES):
                href = link.get("href")
                if href:
                    return urljoin(site_url, href), "html_link"
    return None, "failed"


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
    own_http = http is None
    if own_http:
        http = _make_default_http()
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        nls = list(newsletters) if newsletters is not None else load_newsletters()

        all_signals: list[Signal] = []
        for nl in nls:
            site = (nl.url or "").strip()
            if not site:
                continue

            t0 = time.monotonic()
            feed_url, discovery = discover_feed_url(site, http=http)
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

            signals, info = fetch_feed(feed_url, source_name=nl.name, since=since, http=http)
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
    finally:
        if own_http and http is not None:
            http.close()


# --- Logging ------------------------------------------------------------

def _log_record(rec: dict) -> None:
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
