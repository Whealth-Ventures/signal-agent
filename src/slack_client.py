"""Layer 4: Slack Block Kit digest formatter and webhook poster.

Validates each story URL (HEAD with GET fallback for sites that 405/403 HEAD),
drops invalid URLs, builds a Block Kit payload, POSTs to the configured Slack
Incoming Webhook. Returns a SlackResult; doesn't persist — main.py wraps with
storage.mark_digest_sent / mark_digest_failed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config
from ranker import RankedStory


# --- Public API ---------------------------------------------------------

@dataclass(frozen=True)
class SlackResult:
    sent: bool
    channel_label: str
    stories_sent: int
    stories_dropped_invalid_url: int
    elapsed_seconds: float
    error: str | None = None
    blocks: list[dict] | None = None
    status_code: int | None = None


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _make_default_http() -> httpx.Client:
    return httpx.Client(
        timeout=config.URL_VALIDATION_TIMEOUT_S,
        headers={"User-Agent": "SignalAgent/0.1"},
        follow_redirects=True,
    )


def validate_url(url: str, *, http: httpx.Client | None = None) -> bool:
    own_http = http is None
    if own_http:
        http = _make_default_http()
    try:
        try:
            r = http.request("HEAD", url)
        except httpx.HTTPError:
            return False
        if r.status_code in (403, 405):
            # Some sites block HEAD; retry with GET (don't read body).
            try:
                r = http.get(url)
            except httpx.HTTPError:
                return False
        return 200 <= r.status_code < 400
    finally:
        if own_http and http is not None:
            http.close()


def _escape_mrkdwn(s: str) -> str:
    """Escape Slack mrkdwn special chars in untrusted text (one-liners, domain
    headers). Order matters: '&' must be replaced first."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _group_by_domain(ranked: list[RankedStory]) -> list[dict]:
    """Group ranked stories by domain, preserving the order each domain first
    appears (which is overall rank order). Stories within a group keep rank order."""
    bucket_order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for r in ranked:
        domain = (r.domain or "Other").strip() or "Other"
        if domain not in buckets:
            buckets[domain] = []
            bucket_order.append(domain)
        buckets[domain].append({
            "rank": r.rank,
            "story": r.story,
            "one_liner": r.one_liner or r.story.canonical_title,
            "host": _host(r.story.canonical_url),
        })
    return [{"domain": d, "stories": buckets[d]} for d in bucket_order]


def build_blocks(
    ranked: list[RankedStory],
    *,
    digest_date: str,
    generated_at: datetime | None = None,
) -> list[dict]:
    gen = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Daily Healthcare Signal — {digest_date}",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "_A scannable snapshot — tap any line to dig in._"},
            ],
        },
    ]

    if not ranked:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "No stories made the cut today. The agent ran without errors but had no qualifying signals.",
            },
        })
    else:
        for group in _group_by_domain(ranked):
            lines = [f"*{_escape_mrkdwn(group['domain'].upper())}*"]
            for s in group["stories"]:
                title = _escape_mrkdwn(s["one_liner"])
                url = s["story"].canonical_url
                host = _escape_mrkdwn(s["host"])
                lines.append(f"• <{url}|{title}>")
                lines.append(f"   _{host}_")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            })

    total = len(ranked)
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Generated {gen} UTC · {total} "
                    f"{'story' if total == 1 else 'stories'} · "
                    "Signal Agent · 2070 Health"
                ),
            },
        ],
    })
    return blocks


def post_digest(
    ranked: list[RankedStory],
    *,
    digest_date: str,
    webhook_url: str | None = None,
    http: httpx.Client | None = None,
    skip_url_validation: bool = False,
) -> SlackResult:
    start = time.monotonic()
    url = webhook_url if webhook_url is not None else config.SLACK_WEBHOOK_URL
    channel_label = config.SLACK_CHANNEL_LABEL
    if not url:
        return SlackResult(
            sent=False, channel_label=channel_label,
            stories_sent=0, stories_dropped_invalid_url=0,
            elapsed_seconds=round(time.monotonic() - start, 3),
            error="SLACK_WEBHOOK_URL not configured",
        )

    own_http = http is None
    h = http or _make_default_http()

    try:
        # 1) Validate URLs
        valid: list[RankedStory] = []
        dropped = 0
        if skip_url_validation:
            valid = list(ranked)
        else:
            for r in ranked:
                if validate_url(r.story.canonical_url, http=h):
                    valid.append(r)
                else:
                    dropped += 1

        # Re-number consecutively after drops (1, 2, 3, ...).
        valid = [
            RankedStory(
                story=r.story,
                rank=i + 1,
                one_liner=r.one_liner,
                domain=r.domain,
            )
            for i, r in enumerate(valid)
        ]

        # 2) Build blocks
        blocks = build_blocks(valid, digest_date=digest_date)
        # fallback `text` is shown in notifications + clients that don't render blocks.
        payload = {
            "text": f"Daily Healthcare Signal — {digest_date}",
            "blocks": blocks,
        }

        # 3) POST
        error: str | None = None
        status: int | None = None
        sent_ok = False
        try:
            resp = h.post(url, json=payload)
            status = resp.status_code
            body = (resp.text or "").strip()
            if status == 200 and body == "ok":
                sent_ok = True
            else:
                error = f"HTTP {status}: {body[:200]}"
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"
    finally:
        if own_http:
            h.close()

    elapsed = round(time.monotonic() - start, 3)

    _log({
        "digest_date": digest_date,
        "sent": sent_ok,
        "channel_label": channel_label,
        "stories_sent": len(valid),
        "stories_dropped_invalid_url": dropped,
        "status_code": status,
        "latency_ms": int(elapsed * 1000),
        "error": error,
    })

    return SlackResult(
        sent=sent_ok,
        channel_label=channel_label,
        stories_sent=len(valid),
        stories_dropped_invalid_url=dropped,
        elapsed_seconds=elapsed,
        error=error,
        blocks=blocks,
        status_code=status,
    )


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"slack_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
