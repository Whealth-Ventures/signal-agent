"""src/bot.py — Signal Agent's Slack Q&A bot (Socket Mode), Phase B.

Answers @signal_agent mentions and direct messages, always in a thread, from
the labelled news archive (src/qa.py). A long-running process of its own
(deploy/signal-agent-bot.service): the digest runs never import or wait on it,
and it only ever reads agent.db.

Slack handling is adapted from salesforce-sage/app.py: event-id dedupe, the
thread as chat history, a placeholder that the answer replaces.

Audit trail: data/logs/qa_<date>.jsonl, one line per question.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone

import alerts
import config
import qa

log = logging.getLogger("signal-agent.bot")

PLACEHOLDER = ":mag: Searching the news archive…"
HELP = ("Ask me about healthcare news in the archive, for example: "
        "_What are the biggest healthcare IPOs this month?_")
_CHUNK_LIMIT = 3800
_HISTORY_MAX = 12


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def to_mrkdwn(text: str) -> str:
    """The model writes markdown; Slack wants its own mrkdwn."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", text)
    text = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)
    return re.sub(r"^(\s*)[-\*\+]\s+", r"\1• ", text, flags=re.MULTILINE)


def chunk(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Slack-sized pieces, split on line boundaries."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return [*out, cur] if cur else out or [""]


class _Seen:
    """Slack redelivers events; answer each one once."""
    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def first(self, event_id: str | None) -> bool:
        if not event_id:
            return True
        with self._lock:
            if event_id in self._ids:
                return False
            if len(self._ids) > 5000:
                self._ids.clear()
            self._ids.add(event_id)
            return True


def _history(client, bot_user_id: str, channel: str, thread_ts: str | None) -> list[dict]:
    """Earlier messages in the thread as chat turns, so follow-ups work."""
    if not thread_ts:
        return []
    try:
        msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=30)["messages"][:-1]
    except Exception as e:  # history is a nicety; answer without it
        log.warning("thread history unavailable: %s", e)
        return []
    turns = []
    for m in msgs:
        text = strip_mentions(m.get("text", ""))
        ours = m.get("user") == bot_user_id
        if not text or text == PLACEHOLDER or (m.get("bot_id") and not ours):
            continue
        turns.append({"role": "assistant" if ours else "user", "content": text})
    return turns[-_HISTORY_MAX:]


def _failure_text(e: Exception) -> str:
    if alerts.openai_problem(e) == "billing":
        return ":warning: OpenAI, which I use to answer, is out of credits. Answers resume once credits are added."
    return ":warning: I couldn't answer that just now. Please try again in a minute."


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with (config.LOGS_DIR / f"qa_{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("qa log write failed")


def handle(client, bot_user_id: str, event: dict, *, answer=qa.answer) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    question = strip_mentions(event.get("text", ""))
    if not question:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=HELP)
        return
    placeholder = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=PLACEHOLDER)["ts"]
    t0 = time.monotonic()
    usage, error = {}, None
    try:
        text, usage = answer(question, _history(client, bot_user_id, channel, event.get("thread_ts")))
    except Exception as e:
        log.exception("answer failed")
        text, error = _failure_text(e), f"{type(e).__name__}: {e}"[:300]
    parts = chunk(to_mrkdwn(text))
    client.chat_update(channel=channel, ts=placeholder, text=parts[0])
    for part in parts[1:]:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part,
                                unfurl_links=False, unfurl_media=False)
    _log({"event": "answer", "channel": channel, "user": event.get("user"),
          "question": question, "latency_ms": int((time.monotonic() - t0) * 1000),
          "answer_chars": len(text), "error": error, **usage})


_AUTH_ERRORS = ("invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired")


def _is_auth_error(e: Exception) -> bool:
    """Slack said the token itself is bad, which a restart can't fix."""
    resp = getattr(e, "response", None)
    code = resp.get("error") if hasattr(resp, "get") else None
    return code in _AUTH_ERRORS or any(c in str(e) for c in _AUTH_ERRORS)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (config.SLACK_APP_TOKEN and config.SLACK_BOT_TOKEN):
        # Exit clean: the unit restarts on failure only, so an unconfigured box idles.
        log.warning("SLACK_APP_TOKEN or SLACK_BOT_TOKEN not set: Q&A bot not started")
        return 0

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    try:
        app = App(token=config.SLACK_BOT_TOKEN)
        bot_user_id = app.client.auth_test()["user_id"]
    except Exception as e:
        if _is_auth_error(e):  # a bad token won't fix itself: don't restart-loop
            log.error("Slack rejected SLACK_BOT_TOKEN (%s): Q&A bot not started", e)
            return 0
        raise
    seen = _Seen()

    def spawn(event: dict) -> None:
        threading.Thread(target=handle, args=(app.client, bot_user_id, event), daemon=True).start()

    @app.event("app_mention")
    def on_mention(event, ack):
        ack()
        if event.get("user") != bot_user_id and seen.first(event.get("client_msg_id") or event.get("ts")):
            spawn(event)

    @app.event("message")
    def on_message(event, ack):
        ack()
        # DMs only; channel messages arrive as app_mention. Skip edits, joins
        # and our own posts.
        if (event.get("channel_type") == "im" and event.get("subtype") is None
                and not event.get("bot_id") and event.get("user") != bot_user_id
                and seen.first(event.get("client_msg_id") or event.get("ts"))):
            spawn(event)

    log.info("Signal Agent Q&A bot starting (user %s), archive %s", bot_user_id, config.DB_PATH)
    try:
        SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
    except Exception as e:
        if _is_auth_error(e):
            log.error("Slack rejected SLACK_APP_TOKEN (%s): Q&A bot not started", e)
            return 0
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
