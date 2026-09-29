"""src/alerts.py — a Slack alert when OpenAI fails for a reason no retry fixes:
out of credits, key rejected, or model not available on the key.

Every run needs OpenAI (embeddings for scoring, then the enricher's tagging),
so a dead key means no digest at all, silently: the run crashes before the
post. The alert goes to the channel of the run that hit it, so the people
waiting for the digest see why it's missing and what fixes it.

Fail-soft: an alert that can't post is logged, never raised, so the original
error still surfaces in the journal.
"""
from __future__ import annotations

import httpx
import openai

import config
import slack_client

BILLING_URL = "https://platform.openai.com/settings/organization/billing"

# problem → (headline, what the reader does about it)
_PROBLEMS = {
    "billing": (
        "OpenAI is out of credits",
        f"add credits at <{BILLING_URL}|OpenAI billing>. The next run recovers on its own.",
    ),
    "auth": (
        "The OpenAI API key was rejected",
        "update `OPENAI_API_KEY` in the agent secret, then redeploy.",
    ),
    "model": (
        "An OpenAI model isn't available on this key",
        "check `ENRICH_MODEL` in `src/config.py` and `embedding_model` in tuning.xlsx.",
    ),
}


def openai_problem(exc: BaseException | None) -> str | None:
    """'billing' | 'auth' | 'model' when `exc`, or anything in its cause chain,
    is an OpenAI error that retrying won't fix. None otherwise, including a
    plain rate limit, which is transient."""
    for _ in range(5):
        if exc is None:
            return None
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return "auth"
        if isinstance(exc, openai.RateLimitError) and exc.code == "insufficient_quota":
            return "billing"
        if isinstance(exc, openai.NotFoundError) and exc.code == "model_not_found":
            return "model"
        exc = exc.__cause__ or exc.__context__
    return None


def post_openai_alert(problem: str, *, impact: str, channel_id: str | None) -> bool:
    """Post one alert. `impact` says what didn't happen, e.g. "Today's digest
    could not be built." Returns True if Slack accepted it."""
    headline, fix = _PROBLEMS[problem]
    text = f":rotating_light: *{headline}. {impact}*\n*Need from you:* {fix}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    rec = {"event": "openai_alert", "problem": problem, "channel": channel_id}
    try:
        with httpx.Client(timeout=15.0) as h:
            if config.SLACK_BOT_TOKEN and channel_id:
                ok, status, err, _, _ = slack_client._post_via_api(
                    h=h, bot_token=config.SLACK_BOT_TOKEN, channel_id=channel_id,
                    text=text, blocks=blocks,
                )
            else:
                ok, status, err = slack_client._post_via_webhook(
                    h=h, url=config.SLACK_WEBHOOK_URL, text=text, blocks=blocks,
                )
        rec.update(ok=ok, status=status, error=err)
    except Exception as e:
        ok = False
        rec.update(ok=False, error=f"{type(e).__name__}: {e}")
    try:
        slack_client._log(rec)
    except Exception:
        pass
    return ok


def alert_if_openai_down(exc: BaseException, *, impact: str, channel_id: str | None) -> None:
    """Call from an entrypoint's except block, then re-raise."""
    problem = openai_problem(exc)
    if problem:
        post_openai_alert(problem, impact=impact, channel_id=channel_id)
