"""Layer 4: Slack Block Kit digest formatter and webhook poster.

Consumes a ranker.RankingResult and produces the locked daily format:

  *Daily Healthcare Signal — Wed, 27 May 2026*  ·  22 stories

  *Today's biggest stories*
    • [IND] one-liner (Link)
    • [US]  one-liner (Link)
    ...

  *Venture & IPO* (2)
    • [IND] one-liner (Link)
    • [US]  one-liner (Link)

  ... (per-priority sections; hidden if empty after the top-5 promotion)

  *Other healthcare news* (5)
    • [US]  one-liner (Link)
    ...

Validates each story URL (HEAD with GET fallback) before posting; drops stories
with invalid URLs rather than shipping broken links.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config
from ranker import RankedStory, RankingResult

# Slack Block Kit limits — keep us well clear of them.
MAX_BLOCKS = 48
MAX_SECTION_CHARS = 2900     # actual limit is 3000; small headroom

# Concurrent URL HEAD validations. Production sees 20-40 URLs per digest;
# serial HEAD-with-GET-fallback was a 10-30s blocker.
URL_VALIDATION_CONCURRENCY = 10

# Geo tag prefixes used in bullets. Empty string for global / unknown — per
# user instruction, only India and US get explicit tags.
_GEO_TAG: dict[str, str] = {
    "India": "[IND] ",
    "US":    "[US]  ",
}


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
    slack_ts: str | None = None
    slack_channel: str | None = None


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


async def _validate_url_async(url: str, http: httpx.AsyncClient) -> bool:
    try:
        r = await http.request("HEAD", url)
    except httpx.HTTPError:
        return False
    if r.status_code in (403, 405):
        try:
            r = await http.get(url)
        except httpx.HTTPError:
            return False
    return 200 <= r.status_code < 400


# --- Formatting --------------------------------------------------------

def _escape_mrkdwn(s: str) -> str:
    """Escape Slack mrkdwn special chars in untrusted text. Order matters:
    '&' must be replaced first."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _geo_tag(geo: str | None) -> str:
    return _GEO_TAG.get(geo or "", "")


def _bullet(r: RankedStory) -> str:
    """Render one bullet: `• [GEO] one_liner (Link)`. No source name shown;
    only the (Link) text is hyperlinked."""
    tag = _geo_tag(r.story.geo)
    text = _escape_mrkdwn(r.one_liner or r.story.canonical_title)
    url = r.story.canonical_url
    return f"• {tag}{text} (<{url}|Link>)"


def _priority_display(key: str) -> str:
    for b in config.PRIORITY_BUCKETS:
        if b.key == key:
            return b.display
    return key


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _section_with_header_and_bullets(header: str, bullets: list[str]) -> list[dict]:
    """Emit one or more section blocks. Splits into multiple sections if the
    combined text would exceed MAX_SECTION_CHARS. Header appears only on the
    first block; continuation blocks use the same content."""
    if not bullets:
        return []
    blocks: list[dict] = []
    current = [header]
    current_len = len(header)
    for b in bullets:
        addition = len(b) + 1  # +1 for newline
        if current_len + addition > MAX_SECTION_CHARS and len(current) > 1:
            blocks.append(_section("\n".join(current)))
            current = [b]
            current_len = len(b)
        else:
            current.append(b)
            current_len += addition
    if current:
        blocks.append(_section("\n".join(current)))
    return blocks


def build_blocks(
    ranking: RankingResult,
    *,
    digest_date: str,
    test_mode: bool = False,
) -> list[dict]:
    """Build the Block Kit payload from a RankingResult.

    `test_mode` prepends `[TEST]` to the header so an operator can run a live
    Slack post without the channel mistaking it for the day's real digest.
    """
    total = (
        len(ranking.top_summary)
        + sum(len(v) for v in ranking.by_priority.values())
        + len(ranking.other)
    )
    plural = "story" if total == 1 else "stories"

    title_prefix = "[TEST] " if test_mode else ""
    blocks: list[dict] = [
        _section(
            f"*{title_prefix}Daily Healthcare Signal — "
            f"{_escape_mrkdwn(digest_date)}*"
            f"  ·  {total} {plural}"
        ),
    ]

    if total == 0:
        blocks.append(_section(
            "_No stories made the cut today. The agent ran without errors but "
            "had no qualifying signals._"
        ))
        return blocks

    if ranking.top_summary:
        bullets = [_bullet(r) for r in ranking.top_summary]
        blocks.extend(_section_with_header_and_bullets(
            "*Today's biggest stories*", bullets,
        ))
        blocks.append({"type": "divider"})

    for bucket in config.PRIORITY_BUCKETS:
        items = ranking.by_priority.get(bucket.key, [])
        if not items:
            continue
        header = f"*{_escape_mrkdwn(bucket.display)}* ({len(items)})"
        bullets = [_bullet(r) for r in items]
        blocks.extend(_section_with_header_and_bullets(header, bullets))

    if ranking.other:
        blocks.append({"type": "divider"})
        header = f"*Other healthcare news* ({len(ranking.other)})"
        bullets = [_bullet(r) for r in ranking.other]
        blocks.extend(_section_with_header_and_bullets(header, bullets))

    # Enforce the Slack Block Kit ceiling. If we still overflow, truncate "Other"
    # rather than priority categories.
    while len(blocks) > MAX_BLOCKS and blocks:
        blocks.pop()

    return blocks


# --- URL validation flow ----------------------------------------------

def _all_ranked(ranking: RankingResult) -> list[RankedStory]:
    return (
        list(ranking.top_summary)
        + [r for v in ranking.by_priority.values() for r in v]
        + list(ranking.other)
    )


def _ranking_from_decisions(
    ranking: RankingResult, ok_ids: set[str],
) -> RankingResult:
    top = [r for r in ranking.top_summary if r.story.id in ok_ids]
    by_priority = {
        k: [r for r in v if r.story.id in ok_ids]
        for k, v in ranking.by_priority.items()
    }
    by_priority = {k: v for k, v in by_priority.items() if v}
    other = [r for r in ranking.other if r.story.id in ok_ids]
    return RankingResult(
        top_summary=top,
        by_priority=by_priority,
        other=other,
        candidates_count=ranking.candidates_count,
        used_fallback=ranking.used_fallback,
        cost_usd=ranking.cost_usd,
        elapsed_seconds=ranking.elapsed_seconds,
        flat=tuple(
            top
            + [r for b in config.PRIORITY_BUCKETS for r in by_priority.get(b.key, [])]
            + other
        ),
    )


def _filter_invalid_urls(
    ranking: RankingResult,
    *,
    http: httpx.Client,
    skip: bool,
) -> tuple[RankingResult, int]:
    """Drop ranked stories whose URLs fail HEAD/GET validation. Returns the
    filtered result + count of drops. If `skip` is True, returns input unchanged.
    Uses the sync httpx.Client passed in — this is the path tests exercise."""
    if skip:
        return ranking, 0
    all_items = _all_ranked(ranking)
    ok_ids: set[str] = set()
    dropped = 0
    for r in all_items:
        if validate_url(r.story.canonical_url, http=http):
            ok_ids.add(r.story.id)
        else:
            dropped += 1
    return _ranking_from_decisions(ranking, ok_ids), dropped


async def _filter_invalid_urls_async(
    ranking: RankingResult,
    *,
    skip: bool,
    concurrency: int = URL_VALIDATION_CONCURRENCY,
) -> tuple[RankingResult, int]:
    """Async variant: validates all candidate URLs concurrently under a
    semaphore. Used in production where no sync httpx.Client is injected."""
    if skip:
        return ranking, 0
    all_items = _all_ranked(ranking)
    if not all_items:
        return ranking, 0

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=config.URL_VALIDATION_TIMEOUT_S,
        headers={"User-Agent": "SignalAgent/0.1"},
        follow_redirects=True,
    ) as http:
        async def check(r: RankedStory) -> tuple[str, bool]:
            async with sem:
                ok = await _validate_url_async(r.story.canonical_url, http)
                return r.story.id, ok
        results = await asyncio.gather(*(check(r) for r in all_items))
    ok_ids = {sid for sid, ok in results if ok}
    dropped = sum(1 for _, ok in results if not ok)
    return _ranking_from_decisions(ranking, ok_ids), dropped


# --- Poster -----------------------------------------------------------

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def _post_via_api(
    *,
    h: httpx.Client,
    bot_token: str,
    channel_id: str,
    text: str,
    blocks: list[dict],
) -> tuple[bool, int | None, str | None, str | None, str | None]:
    """POST to chat.postMessage. Returns (ok, http_status, error, ts, channel)."""
    try:
        resp = h.post(
            _SLACK_POST_URL,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel_id, "text": text, "blocks": blocks},
        )
    except httpx.HTTPError as e:
        return False, None, f"{type(e).__name__}: {e}", None, None

    status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        return False, status, f"HTTP {status}: non-JSON response", None, None

    if status == 200 and data.get("ok"):
        return True, status, None, data.get("ts"), data.get("channel")
    err = data.get("error") or f"HTTP {status}"
    return False, status, f"slack api error: {err}", None, None


def _post_via_webhook(
    *,
    h: httpx.Client,
    url: str,
    text: str,
    blocks: list[dict],
) -> tuple[bool, int | None, str | None]:
    """POST to incoming webhook. Returns (ok, http_status, error). No ts."""
    try:
        resp = h.post(url, json={"text": text, "blocks": blocks})
    except httpx.HTTPError as e:
        return False, None, f"{type(e).__name__}: {e}"
    status = resp.status_code
    body = (resp.text or "").strip()
    if status == 200 and body == "ok":
        return True, status, None
    return False, status, f"HTTP {status}: {body[:200]}"


def post_digest(
    ranking: RankingResult,
    *,
    digest_date: str,
    webhook_url: str | None = None,
    bot_token: str | None = None,
    channel_id: str | None = None,
    http: httpx.Client | None = None,
    skip_url_validation: bool = False,
    test_mode: bool = False,
) -> SlackResult:
    start = time.monotonic()
    channel_label = config.SLACK_CHANNEL_LABEL

    # Prefer chat.postMessage when bot token + channel id are configured (gives
    # us a message ts to join Slack reactions back to the digest). Fall back to
    # incoming webhook otherwise.
    bt = bot_token if bot_token is not None else config.SLACK_BOT_TOKEN
    ch = channel_id if channel_id is not None else config.SLACK_CHANNEL_ID
    wh = webhook_url if webhook_url is not None else config.SLACK_WEBHOOK_URL
    use_api = bool(bt and ch)
    if not use_api and not wh:
        return SlackResult(
            sent=False, channel_label=channel_label,
            stories_sent=0, stories_dropped_invalid_url=0,
            elapsed_seconds=round(time.monotonic() - start, 3),
            error="No Slack transport configured (need SLACK_BOT_TOKEN+SLACK_CHANNEL_ID or SLACK_WEBHOOK_URL)",
        )

    own_http = http is None
    h = http or _make_default_http()
    blocks: list[dict] = []
    sent_ok = False
    error: str | None = None
    status: int | None = None
    stories_sent = 0
    dropped = 0
    slack_ts: str | None = None
    slack_channel: str | None = None

    try:
        if http is None:
            filtered, dropped = asyncio.run(_filter_invalid_urls_async(
                ranking, skip=skip_url_validation,
            ))
        else:
            filtered, dropped = _filter_invalid_urls(
                ranking, http=h, skip=skip_url_validation,
            )
        stories_sent = (
            len(filtered.top_summary)
            + sum(len(v) for v in filtered.by_priority.values())
            + len(filtered.other)
        )
        blocks = build_blocks(filtered, digest_date=digest_date, test_mode=test_mode)
        text_prefix = "[TEST] " if test_mode else ""
        text = f"{text_prefix}Daily Healthcare Signal — {digest_date}"

        if use_api:
            sent_ok, status, error, slack_ts, slack_channel = _post_via_api(
                h=h, bot_token=bt, channel_id=ch, text=text, blocks=blocks,
            )
        else:
            sent_ok, status, error = _post_via_webhook(
                h=h, url=wh, text=text, blocks=blocks,
            )
    finally:
        if own_http:
            h.close()

    elapsed = round(time.monotonic() - start, 3)

    _log({
        "digest_date": digest_date,
        "sent": sent_ok,
        "channel_label": channel_label,
        "transport": "chat.postMessage" if use_api else "webhook",
        "stories_sent": stories_sent,
        "stories_dropped_invalid_url": dropped,
        "status_code": status,
        "slack_ts": slack_ts,
        "slack_channel": slack_channel,
        "block_count": len(blocks),
        "latency_ms": int(elapsed * 1000),
        "error": error,
    })

    return SlackResult(
        sent=sent_ok,
        channel_label=channel_label,
        stories_sent=stories_sent,
        stories_dropped_invalid_url=dropped,
        elapsed_seconds=elapsed,
        error=error,
        blocks=blocks,
        status_code=status,
        slack_ts=slack_ts,
        slack_channel=slack_channel,
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
