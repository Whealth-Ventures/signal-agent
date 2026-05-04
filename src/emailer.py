"""Layer 4: HTML+text email digest formatter and SMTP sender.

Validates each story URL (HEAD with GET fallback for sites that 405/403 HEAD),
drops invalid URLs, renders via Jinja2, sends via SMTP. Returns an EmailerResult;
doesn't persist — main.py wraps with storage.mark_digest_sent / mark_digest_failed.
"""
from __future__ import annotations

import json
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import httpx
from jinja2 import Environment, select_autoescape

import config
from ranker import RankedStory


# --- Templates (inline) -------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Daily Healthcare Signal — {{ digest_date }}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 720px; margin: 0 auto; padding: 24px; color: #1a1a1a; line-height: 1.5;">
<h1 style="font-size: 22px; margin: 0 0 4px 0;">Daily Healthcare Signal</h1>
<div style="color: #666; font-size: 14px; margin-bottom: 24px;">{{ digest_date }}</div>

{% if not ranked %}
<p style="color: #444;">No stories made the cut today. The agent ran without errors but had no qualifying signals.</p>
{% else %}
{% for r in ranked %}
<div style="margin-bottom: 24px;">
  <div style="color: #999; font-size: 12px; margin-bottom: 4px;">
    #{{ r.rank }} · {{ r.host }}
  </div>
  <a href="{{ r.story.canonical_url }}" style="font-size: 17px; font-weight: 600; color: #0a66c2; text-decoration: none;">{{ r.story.canonical_title }}</a>
  {% if r.reasoning %}
  <div style="color: #444; font-size: 14px; margin-top: 6px;">{{ r.reasoning }}</div>
  {% endif %}
  {% if r.story.canonical_summary %}
  <div style="color: #666; font-size: 13px; margin-top: 4px;">{{ r.story.canonical_summary }}</div>
  {% endif %}
</div>
{% endfor %}
{% endif %}

<hr style="border: none; border-top: 1px solid #e5e5e5; margin: 32px 0 16px;">
<div style="color: #999; font-size: 12px;">
  Generated {{ generated_at }} UTC · {{ ranked|length }} {{ "story" if ranked|length == 1 else "stories" }}<br>
  Signal Agent · 2070 Health
</div>
</body>
</html>
"""

_TEXT_TEMPLATE = """Daily Healthcare Signal — {{ digest_date }}

{% if not ranked %}
No stories made the cut today. The agent ran without errors but had no qualifying signals.
{% else %}
{% for r in ranked %}
#{{ r.rank }}  {{ r.host }} — {{ r.story.canonical_title }}
    {{ r.story.canonical_url }}
{% if r.reasoning %}    {{ r.reasoning }}
{% endif %}
{% endfor %}
{% endif %}
Generated {{ generated_at }} UTC · {{ ranked|length }} {{ "story" if ranked|length == 1 else "stories" }}
Signal Agent · 2070 Health
"""


def _make_env() -> Environment:
    return Environment(
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


# --- Public API ---------------------------------------------------------

@dataclass(frozen=True)
class EmailerResult:
    sent: bool
    recipients: tuple[str, ...]
    stories_sent: int
    stories_dropped_invalid_url: int
    elapsed_seconds: float
    error: str | None = None
    rendered_html: str = ""
    rendered_text: str = ""


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _make_default_http() -> httpx.Client:
    return httpx.Client(
        timeout=config.URL_VALIDATION_TIMEOUT_S,
        headers={"User-Agent": "SignalAgent/0.1 (URL validator)"},
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


def render_digest(
    ranked: list[RankedStory],
    *,
    digest_date: str,
    generated_at: datetime | None = None,
) -> tuple[str, str]:
    env = _make_env()
    gen = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    ctx = {
        "digest_date": digest_date,
        "generated_at": gen,
        "ranked": [
            {
                "rank": r.rank,
                "story": r.story,
                "reasoning": r.reasoning,
                "host": _host(r.story.canonical_url),
            }
            for r in ranked
        ],
    }
    html = env.from_string(_HTML_TEMPLATE).render(**ctx)
    text = env.from_string(_TEXT_TEMPLATE).render(**ctx)
    return html, text


def send_digest(
    ranked: list[RankedStory],
    *,
    digest_date: str,
    recipients: Iterable[str] | None = None,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
    http: httpx.Client | None = None,
    skip_url_validation: bool = False,
) -> EmailerResult:
    start = time.monotonic()
    rcpts = tuple(recipients) if recipients is not None else config.DIGEST_RECIPIENTS
    if not rcpts:
        return EmailerResult(
            sent=False, recipients=(), stories_sent=0,
            stories_dropped_invalid_url=0,
            elapsed_seconds=round(time.monotonic() - start, 3),
            error="no recipients configured",
        )

    # 1) Validate URLs
    valid: list[RankedStory] = []
    dropped = 0
    if skip_url_validation:
        valid = list(ranked)
    else:
        own_http = http is None
        h = http or _make_default_http()
        try:
            for r in ranked:
                if validate_url(r.story.canonical_url, http=h):
                    valid.append(r)
                else:
                    dropped += 1
        finally:
            if own_http:
                h.close()

    # Re-number consecutively after drops (1, 2, 3, ...).
    valid = [
        RankedStory(story=r.story, rank=i + 1, reasoning=r.reasoning)
        for i, r in enumerate(valid)
    ]

    # 2) Render
    html, text = render_digest(valid, digest_date=digest_date)

    # 3) Compose
    msg = EmailMessage()
    msg["Subject"] = f"Healthcare Signal — {digest_date}"
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(rcpts)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    # 4) Send
    factory = smtp_factory or (lambda: smtplib.SMTP(
        config.SMTP_HOST, config.SMTP_PORT, timeout=30,
    ))
    error: str | None = None
    sent_ok = False
    try:
        smtp = factory()
        try:
            smtp.starttls()
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg, from_addr=config.SMTP_FROM, to_addrs=list(rcpts))
            sent_ok = True
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    elapsed = round(time.monotonic() - start, 3)

    _log({
        "digest_date": digest_date,
        "sent": sent_ok,
        "recipients": list(rcpts),
        "stories_sent": len(valid),
        "stories_dropped_invalid_url": dropped,
        "latency_ms": int(elapsed * 1000),
        "error": error,
    })

    return EmailerResult(
        sent=sent_ok,
        recipients=rcpts,
        stories_sent=len(valid),
        stories_dropped_invalid_url=dropped,
        elapsed_seconds=elapsed,
        error=error,
        rendered_html=html,
        rendered_text=text,
    )


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"emailer_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
