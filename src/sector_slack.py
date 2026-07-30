"""Slack formatter + poster for the sector digest.

One message per sector: a header line, then event-type groups (Fundraising,
Clinical & Regulatory, Policy/Legal, Product/Partnerships) each a bullet list
of one-liners — matching the reference roundup format. Reuses slack_client's
Block Kit helpers, URL validation, and transports so behaviour (mrkdwn escaping,
section splitting, unfurl suppression, HEAD/GET link checks) stays identical to
the daily digest.
"""
from __future__ import annotations

import time

import httpx

import config
import slack_client
from sector_ranker import SectorRankingResult
from query_planner import Sector
from slack_client import (
    MAX_BLOCKS,
    SlackResult,
    _bullet,
    _escape_mrkdwn,
    _make_default_http,
    _post_via_api,
    _post_via_webhook,
    _section,
    _section_with_header_and_bullets,
    validate_url,
)


def build_sector_blocks(
    result: SectorRankingResult,
    sector: Sector,
    *,
    date_label: str,
    test_mode: bool = False,
) -> list[dict]:
    prefix = "[TEST] " if test_mode else ""
    portco = f" · {_escape_mrkdwn(sector.portco)}" if sector.portco else ""
    plural = "item" if result.total == 1 else "items"
    blocks: list[dict] = [
        _section(
            f"*{prefix}{_escape_mrkdwn(sector.display)} — sector roundup*"
            f"{portco}  ·  {result.total} {plural}  ·  {_escape_mrkdwn(date_label)}"
        ),
    ]
    if result.summary:
        blocks.append(_section(_escape_mrkdwn(result.summary)))
    if result.total == 0:
        blocks.append(_section(
            "_No substantive sector news in the last 14 days._"
        ))
        return blocks
    for display, items in result.groups:
        header = f"*{_escape_mrkdwn(display)}* ({len(items)})"
        bullets = [_bullet(r) for r in items]
        blocks.extend(_section_with_header_and_bullets(header, bullets))
    while len(blocks) > MAX_BLOCKS and blocks:
        blocks.pop()
    return blocks


def _filter_invalid_urls(
    result: SectorRankingResult, *, http: httpx.Client, skip: bool,
) -> tuple[SectorRankingResult, int]:
    if skip:
        return result, 0
    dropped = 0
    new_groups: list[tuple[str, list]] = []
    for display, items in result.groups:
        kept = []
        for r in items:
            if validate_url(r.story.canonical_url, http=http):
                kept.append(r)
            else:
                dropped += 1
        if kept:
            new_groups.append((display, kept))
    flat = tuple(r for _, items in new_groups for r in items)
    return (
        SectorRankingResult(
            sector_key=result.sector_key,
            groups=new_groups,
            candidates_count=result.candidates_count,
            used_fallback=result.used_fallback,
            cost_usd=result.cost_usd,
            elapsed_seconds=result.elapsed_seconds,
            summary=result.summary,
            flat=flat,
        ),
        dropped,
    )


def post_sector_digest(
    result: SectorRankingResult,
    sector: Sector,
    *,
    digest_date: str,
    date_label: str,
    channel_id: str | None = None,
    webhook_url: str | None = None,
    channel_label: str | None = None,
    http: httpx.Client | None = None,
    skip_url_validation: bool = False,
    test_mode: bool = False,
) -> SlackResult:
    start = time.monotonic()
    channel_label = channel_label or config.SLACK_CHANNEL_LABEL_SECTOR

    bt = config.SLACK_BOT_TOKEN
    ch = channel_id if channel_id is not None else config.SLACK_CHANNEL_ID_SECTOR
    wh = webhook_url if webhook_url is not None else config.SLACK_WEBHOOK_URL_SECTOR
    use_api = bool(bt and ch)
    if not use_api and not wh:
        return SlackResult(
            sent=False, channel_label=channel_label, stories_sent=0,
            stories_dropped_invalid_url=0,
            elapsed_seconds=round(time.monotonic() - start, 3),
            error="No Slack transport configured (need SLACK_BOT_TOKEN+SLACK_CHANNEL_ID_SECTOR or SLACK_WEBHOOK_URL[_SECTOR])",
        )

    own_http = http is None
    h = http or _make_default_http()
    error: str | None = None
    status: int | None = None
    slack_ts: str | None = None
    slack_channel: str | None = None
    sent_ok = False
    try:
        filtered, dropped = _filter_invalid_urls(
            result, http=h, skip=skip_url_validation,
        )
        blocks = build_sector_blocks(
            filtered, sector, date_label=date_label, test_mode=test_mode,
        )
        prefix = "[TEST] " if test_mode else ""
        text = f"{prefix}{sector.display} — sector roundup ({date_label})"
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

    return SlackResult(
        sent=sent_ok,
        channel_label=channel_label,
        stories_sent=filtered.total,
        stories_dropped_invalid_url=dropped,
        elapsed_seconds=round(time.monotonic() - start, 3),
        error=error,
        blocks=blocks,
        status_code=status,
        slack_ts=slack_ts,
        slack_channel=slack_channel,
    )
