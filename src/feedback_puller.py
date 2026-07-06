"""Pull Slack-event objects from S3 into local SQLite.

The admin app's /api/slack/events receiver writes one object per Slack event
(reaction_added, reaction_removed, message.channels) under events/YYYY-MM-DD/
in the FEEDBACK_S3_BUCKET. This module lists what's new since the last cursor,
downloads each object, joins to the digests table by slack_ts to recover
digest_id, and persists rows to the `feedback` table.

Idempotent: event_id is the primary key, so re-runs are no-ops.

(Formerly backed by Vercel Blob; migrated to S3 for the AWS deployment.)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3

import config
import storage

_PREFIX = "events/"


@dataclass(frozen=True)
class PullResult:
    listed: int
    new_events: int
    linked_to_digest: int
    cursor_advanced_to: str | None


def _bucket() -> str:
    if not config.FEEDBACK_S3_BUCKET:
        raise RuntimeError("FEEDBACK_S3_BUCKET not set in env")
    return config.FEEDBACK_S3_BUCKET


def _client():
    return boto3.client("s3", region_name=config.AWS_REGION or None)


def _list_objects(s3, bucket: str) -> list[dict]:
    """List all objects under events/. Follows pagination via ContinuationToken."""
    out: list[dict] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": _PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for o in resp.get("Contents", []):
            out.append({"key": o["Key"], "last_modified": o["LastModified"]})
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out


def _download(s3, bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read())


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


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
    from a stored payload. The puller stores the full raw payload too."""
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
    s3=None,
    conn: sqlite3.Connection | None = None,
) -> PullResult:
    """Pull new events from S3 into the feedback table. Idempotent."""
    client = s3 or _client()
    bucket = _bucket()
    own_conn = conn is None
    c = conn or storage.connect()
    try:
        storage.init_db(conn=c)
        cursor = _get_cursor(c)

        objs = _list_objects(client, bucket)
        # Sort by last_modified ascending so the cursor advances monotonically.
        objs.sort(key=lambda o: o["last_modified"])

        new_count = 0
        linked = 0
        max_received_at = cursor

        for o in objs:
            lm = _iso(o["last_modified"])
            if cursor and lm <= cursor:
                continue
            payload = _download(client, bucket, o["key"])
            event_id = payload.get("event_id")
            received_at = payload.get("received_at") or lm
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
            if not max_received_at or lm > max_received_at:
                max_received_at = lm

        if max_received_at and max_received_at != cursor:
            _set_cursor(c, max_received_at)
        if own_conn:
            c.commit()

        return PullResult(
            listed=len(objs),
            new_events=new_count,
            linked_to_digest=linked,
            cursor_advanced_to=max_received_at,
        )
    finally:
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
