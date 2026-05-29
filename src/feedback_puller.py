"""Pull Slack-event blobs from Vercel Blob into local SQLite.

The admin app's /api/slack/events receiver writes one blob per Slack event
(reaction_added, reaction_removed, message.channels) under events/YYYY-MM-DD/.
This module lists what's new since the last cursor, downloads each blob,
joins to the digests table by slack_ts to recover digest_id, and persists
rows to the `feedback` table.

Idempotent: event_id is the primary key, so re-runs are no-ops.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import config
import storage

_LIST_URL = "https://vercel.com/api/blob"
_LIST_PREFIX = "events/"


@dataclass(frozen=True)
class PullResult:
    listed: int
    new_events: int
    linked_to_digest: int
    cursor_advanced_to: str | None


def _headers() -> dict[str, str]:
    token = config.BLOB_READ_WRITE_TOKEN
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN not set in .env")
    return {"Authorization": f"Bearer {token}"}


def _list_blobs(http: httpx.Client) -> list[dict]:
    """List all blobs under events/. Follows pagination via `cursor`."""
    blobs: list[dict] = []
    params: dict[str, str] = {"prefix": _LIST_PREFIX}
    while True:
        r = http.get(_LIST_URL, headers=_headers(), params=params)
        r.raise_for_status()
        data = r.json()
        blobs.extend(data.get("blobs") or [])
        if not data.get("hasMore"):
            break
        cursor = data.get("cursor")
        if not cursor:
            break
        params = {"prefix": _LIST_PREFIX, "cursor": cursor}
    return blobs


def _download(http: httpx.Client, url: str) -> dict:
    r = http.get(url, headers=_headers())
    r.raise_for_status()
    return r.json()


def _get_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_received_at FROM feedback_cursor WHERE id=1"
    ).fetchone()
    return row["last_received_at"] if row else None


def _set_cursor(conn: sqlite3.Connection, received_at: str) -> None:
    conn.execute(
        "INSERT INTO feedback_cursor (id, last_received_at) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_received_at=excluded.last_received_at",
        (received_at,),
    )


def _digest_id_for_slack_ts(
    conn: sqlite3.Connection, slack_ts: str | None, slack_channel: str | None,
) -> str | None:
    """Look up the digest a reaction targets. We match on slack_ts first
    (uniqueness guaranteed within a workspace) and ignore slack_channel for
    flexibility, but the channel is captured for audit."""
    if not slack_ts:
        return None
    row = conn.execute(
        "SELECT id FROM digests WHERE slack_ts=? LIMIT 1", (slack_ts,),
    ).fetchone()
    return row["id"] if row else None


def _extract(ev: dict) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Return (event_type, slack_user, reaction, slack_ts, slack_channel)
    from a stored blob payload. The puller stores the full raw payload too."""
    event_type = ev.get("type") or "unknown"
    inner = ev.get("event") or {}
    user = inner.get("user")
    reaction = inner.get("reaction")
    item = inner.get("item") or {}
    slack_ts = item.get("ts") or inner.get("ts")
    slack_channel = item.get("channel") or inner.get("channel")
    return event_type, user, reaction, slack_ts, slack_channel


def pull(
    *,
    http: httpx.Client | None = None,
    conn: sqlite3.Connection | None = None,
) -> PullResult:
    """Pull new events from Vercel Blob into the feedback table. Idempotent."""
    own_http = http is None
    h = http or httpx.Client(timeout=30.0)
    own_conn = conn is None
    c = conn or storage.connect()
    try:
        storage.init_db(conn=c)
        cursor = _get_cursor(c)

        blobs = _list_blobs(h)
        # Sort by uploadedAt ascending so cursor advances monotonically.
        blobs.sort(key=lambda b: b.get("uploadedAt") or "")

        new_count = 0
        linked = 0
        max_received_at = cursor

        for b in blobs:
            uploaded_at = b.get("uploadedAt") or ""
            if cursor and uploaded_at <= cursor:
                continue
            payload = _download(h, b["url"])
            event_id = payload.get("event_id")
            received_at = payload.get("received_at") or uploaded_at
            if not event_id:
                continue
            event_type, user, reaction, slack_ts, slack_channel = _extract(payload)
            digest_id = _digest_id_for_slack_ts(c, slack_ts, slack_channel)
            try:
                c.execute(
                    "INSERT INTO feedback "
                    "(event_id, received_at, event_type, slack_user, reaction, "
                    " slack_ts, slack_channel, digest_id, raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, received_at, event_type, user, reaction,
                        slack_ts, slack_channel, digest_id,
                        json.dumps(payload, separators=(",", ":")),
                    ),
                )
                new_count += 1
                if digest_id:
                    linked += 1
            except sqlite3.IntegrityError:
                # Already pulled (event_id PRIMARY KEY collision). Skip.
                pass
            if uploaded_at and (not max_received_at or uploaded_at > max_received_at):
                max_received_at = uploaded_at

        if max_received_at and max_received_at != cursor:
            _set_cursor(c, max_received_at)
        if own_conn:
            c.commit()

        return PullResult(
            listed=len(blobs),
            new_events=new_count,
            linked_to_digest=linked,
            cursor_advanced_to=max_received_at,
        )
    finally:
        if own_http:
            h.close()
        if own_conn:
            c.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


if __name__ == "__main__":
    result = pull()
    print(json.dumps({
        "listed": result.listed,
        "new_events": result.new_events,
        "linked_to_digest": result.linked_to_digest,
        "cursor_advanced_to": result.cursor_advanced_to,
        "ran_at": _utcnow_iso(),
    }, indent=2))
