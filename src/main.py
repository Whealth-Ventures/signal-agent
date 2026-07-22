"""src/main.py — orchestrator. Run from cron at 8am IST.

Wires the daily pipeline together: setup → fetch (Perplexity + RSS) → save →
score → rank → console + Slack → persist digest. Returns exit code 0 if the
Slack post was sent, 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import chromadb
import httpx

import config
import ranker
import scorer
import slack_client
import storage
from content_indexer import (
    COLLECTION_NAME,
    Embedder,
    _default_chroma_client,
    reindex_content_dir,
)
from models import Signal
from perplexity_client import (
    ChatResponse,
    PerplexityClient,
    RateLimitExceeded,
)
from query_planner import QueryPlan, build_query_plans
from ranker import _extract_json
from rss_fetcher import fetch_all_newsletters, fetch_all_voice_feeds

PERPLEXITY_HEADROOM = 2  # leave room for ranker (1 call) + 1 retry/buffer

# Concurrent in-flight Perplexity fetches. Cap is 60/day, ranker is 1 call,
# polite enough not to spam the API.
PERPLEXITY_FETCH_CONCURRENCY = 5


@dataclass(frozen=True)
class PipelineStats:
    perplexity_signals: int
    rss_signals: int
    signals_saved: int
    stories_created: int
    ranked_count: int
    digest_sent: bool
    elapsed_seconds: float


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"pipeline_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _progress(msg: str) -> None:
    """Print a live progress line to stderr (won't pollute stdout digest output)."""
    print(msg, file=sys.stderr, flush=True)


# --- Helpers ------------------------------------------------------------

def _parse_iso_or_now(s: object) -> datetime:
    if not isinstance(s, str) or not s:
        return datetime.now(timezone.utc)
    try:
        # Handle 'Z' suffix
        v = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def parse_perplexity_response(plan: QueryPlan, response: ChatResponse) -> list[Signal]:
    """Parse the model's JSON output into Signals; fall back to citations on parse failure."""
    parsed = _extract_json(response.text)
    out: list[Signal] = []
    source = f"Perplexity:{plan.id}"

    if parsed and isinstance(parsed.get("stories"), list):
        for item in parsed["stories"]:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url:
                continue
            out.append(Signal(
                source=source,
                source_type="perplexity",
                title=title,
                url=url,
                published_at=_parse_iso_or_now(item.get("published")),
                summary=(item.get("summary") or "").strip()[:500],
                raw={
                    "plan_id": plan.id,
                    "bucket": plan.bucket,
                    "priority_bucket": plan.priority_bucket,
                    "geo": plan.geography,
                    "track": plan.track,
                },
            ))
        return out

    # Fallback: each citation becomes a low-info Signal so we don't lose the URLs entirely.
    for url in response.citations:
        if not url:
            continue
        out.append(Signal(
            source=source,
            source_type="perplexity",
            title=f"[{plan.bucket}] (unparsed response)",
            url=url,
            published_at=datetime.now(timezone.utc),
            summary=response.text[:500],
            raw={
                "plan_id": plan.id,
                "priority_bucket": plan.priority_bucket,
                "geo": plan.geography,
                "track": plan.track,
                "fallback": True,
            },
        ))
    return out


def fetch_perplexity(
    client: PerplexityClient,
    plans: list[QueryPlan],
    *,
    headroom: int = PERPLEXITY_HEADROOM,
    recency: str | None = None,
) -> list[Signal]:
    """Iterate plans; collect Signals. Per-plan failures are logged + skipped.

    Stops early when remaining_today drops to `headroom` so the ranker call
    later in the pipeline still has budget.

    `recency` overrides the Perplexity search window (default 'day'); the weekly
    Sector Agent passes 'week'.
    """
    out: list[Signal] = []
    n = len(plans)
    width = len(str(n))
    for i, plan in enumerate(plans, start=1):
        if client.remaining_today <= headroom:
            _log({
                "step": "perplexity_fetch_capped",
                "remaining": client.remaining_today,
                "headroom": headroom,
                "skipped_plan": plan.id,
            })
            _progress(
                f"  [{i:>{width}}/{n}] {plan.id:<40}  capped (remaining<={headroom})"
            )
            break
        try:
            t0 = time.monotonic()
            # Pass recency only when overridden, so test doubles with the legacy
            # search_recent(plan) signature keep working (daily path is None).
            resp = (client.search_recent(plan, recency=recency)
                    if recency is not None else client.search_recent(plan))
            signals = parse_perplexity_response(plan, resp)
            out.extend(signals)
            _progress(
                f"  [{i:>{width}}/{n}] {plan.id:<40}  "
                f"{len(signals):>2} stories  {int((time.monotonic() - t0) * 1000):>5}ms"
            )
        except RateLimitExceeded as e:
            _log({"step": "perplexity_fetch_rate_limit",
                  "plan": plan.id, "error": str(e)})
            _progress(f"  [{i:>{width}}/{n}] {plan.id:<40}  rate-limited, stopping")
            break
        except Exception as e:
            _log({"step": "perplexity_fetch_plan_failed",
                  "plan": plan.id, "error": f"{type(e).__name__}: {e}"})
            _progress(
                f"  [{i:>{width}}/{n}] {plan.id:<40}  FAILED ({type(e).__name__})"
            )
            continue
    return out


async def fetch_perplexity_async(
    client: PerplexityClient,
    plans: list[QueryPlan],
    *,
    headroom: int = PERPLEXITY_HEADROOM,
    concurrency: int = PERPLEXITY_FETCH_CONCURRENCY,
    recency: str | None = None,
) -> list[Signal]:
    """Concurrent variant of fetch_perplexity. Preserves the headroom early-stop
    and per-plan failure isolation. Cap accounting is checked atomically inside
    each task via client._check_cap().

    `recency` overrides the Perplexity search window (default 'day'); the weekly
    Sector Agent passes 'week'."""
    out: list[Signal] = []
    n = len(plans)
    width = len(str(n))
    sem = asyncio.Semaphore(concurrency)
    stop = asyncio.Event()

    async def one(i: int, plan: QueryPlan) -> None:
        if stop.is_set():
            return
        async with sem:
            # Re-check after the semaphore acquire — counter may have moved.
            if stop.is_set():
                return
            if client.remaining_today <= headroom:
                stop.set()
                _log({
                    "step": "perplexity_fetch_capped",
                    "remaining": client.remaining_today,
                    "headroom": headroom,
                    "skipped_plan": plan.id,
                })
                _progress(
                    f"  [{i:>{width}}/{n}] {plan.id:<40}  capped (remaining<={headroom})"
                )
                return
            try:
                t0 = time.monotonic()
                resp = await (
                    client.search_recent_async(plan, recency=recency)
                    if recency is not None else client.search_recent_async(plan)
                )
                signals = parse_perplexity_response(plan, resp)
                out.extend(signals)
                _progress(
                    f"  [{i:>{width}}/{n}] {plan.id:<40}  "
                    f"{len(signals):>2} stories  "
                    f"{int((time.monotonic() - t0) * 1000):>5}ms"
                )
            except RateLimitExceeded as e:
                _log({"step": "perplexity_fetch_rate_limit",
                      "plan": plan.id, "error": str(e)})
                _progress(f"  [{i:>{width}}/{n}] {plan.id:<40}  rate-limited, stopping")
                stop.set()
            except Exception as e:
                _log({"step": "perplexity_fetch_plan_failed",
                      "plan": plan.id, "error": f"{type(e).__name__}: {e}"})
                _progress(
                    f"  [{i:>{width}}/{n}] {plan.id:<40}  FAILED ({type(e).__name__})"
                )

    try:
        await asyncio.gather(*(one(i, p) for i, p in enumerate(plans, start=1)))
    finally:
        await client.aclose()
    return out


def ensure_content_indexed(
    *,
    chroma_client: chromadb.api.ClientAPI | None = None,
    embedder: Embedder | None = None,
    content_dir: Path | None = None,
) -> None:
    """First-run hook: if Chroma's content_corpus is empty, run reindex."""
    client = chroma_client or _default_chroma_client()
    try:
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"},
        )
        count = collection.count()
    except Exception:
        count = 0

    if count == 0:
        _log({"step": "content_index_first_run", "starting_count": 0})
        stats = reindex_content_dir(
            chroma_client=client,
            embedder=embedder,
            content_dir=content_dir,
        )
        _log({"step": "content_index_done", **asdict(stats)})


def _geo_tag(geo: str | None) -> str:
    """Compact tag used in console preview: every story is tagged India/US/Global
    (Global is the default for missing/unknown geo)."""
    if geo == "India":
        return "[IND] "
    if geo == "US":
        return "[US] "
    return "[GLOBAL] "


def _priority_display(key: str | None) -> str:
    for b in config.PRIORITY_BUCKETS:
        if b.key == key:
            return b.display
    return "Other"


@dataclass(frozen=True)
class GeoTarget:
    """Resolved routing for one --geo value: which channel to post to and which
    story geos belong there."""
    geo: str                       # "india" | "us" | "both"
    channel_id: str
    channel_label: str
    allowed: frozenset[str] | None  # None = keep all (legacy "both")


def _resolve_geo(geo: str) -> GeoTarget:
    geo = (geo or "both").lower()
    if geo == "india":
        return GeoTarget("india", config.SLACK_CHANNEL_ID_INDIA,
                         config.SLACK_CHANNEL_LABEL_INDIA,
                         frozenset({"India", "Global"}))
    if geo == "us":
        return GeoTarget("us", config.SLACK_CHANNEL_ID_US,
                         config.SLACK_CHANNEL_LABEL_US,
                         frozenset({"US", "Global"}))
    return GeoTarget("both", config.SLACK_CHANNEL_ID,
                     config.SLACK_CHANNEL_LABEL, None)


def print_digest_to_console(ranking: ranker.RankingResult, digest_date: str) -> None:
    """Preview of the locked Slack layout: top-5 summary, then per-category
    sections (hidden when empty), then Other at the bottom."""
    bar = "=" * 72
    total = (
        len(ranking.top_summary)
        + sum(len(v) for v in ranking.by_priority.values())
        + len(ranking.other)
    )
    print()
    print(bar)
    print(f"Daily Healthcare Signal — {digest_date}  ·  {total} stories")
    print(bar)
    if total == 0:
        print("(no stories qualified today)")
        return

    if ranking.top_summary:
        print("\nToday's biggest stories")
        for r in ranking.top_summary:
            tag = _geo_tag(r.story.geo)
            print(f"  • {tag}{r.one_liner} ({r.story.canonical_url})")

    for bucket in config.PRIORITY_BUCKETS:
        items = ranking.by_priority.get(bucket.key, [])
        if not items:
            continue
        print(f"\n{bucket.display} ({len(items)})")
        for r in items:
            tag = _geo_tag(r.story.geo)
            print(f"  • {tag}{r.one_liner} ({r.story.canonical_url})")

    if ranking.other:
        print(f"\nOther healthcare news ({len(ranking.other)})")
        for r in ranking.other:
            tag = _geo_tag(r.story.geo)
            print(f"  • {tag}{r.one_liner} ({r.story.canonical_url})")

    print()
    if ranking.used_fallback:
        print("(used score-based fallback — ranker call failed or output was unparseable)")


# --- Pipeline -----------------------------------------------------------

# Hard cap on how long the pipeline will idle waiting for the scheduled post
# time. Protects against a mis-timed trigger (or a huge cron slip) parking the
# job for hours — past this gap we just post as soon as we're ready. Sized for
# the US timer, which fires at a fixed UTC while 08:00 America/New_York moves an
# hour across DST: firing ~10 min before the EDT instant leaves up to a ~70 min
# hold until the EST instant, so the cap must exceed that.
POST_AT_MAX_WAIT_S = 90 * 60


def compute_post_at(
    spec: str | None, *, now_utc: datetime | None = None, tz: str | None = None,
) -> datetime | None:
    """Resolve a 'HH:MM' post-time (in `tz`, default config.DIGEST_TZ, for today)
    to a UTC instant. Returns None if `spec` is blank or unparseable."""
    if not spec or not spec.strip():
        return None
    try:
        hh, mm = spec.strip().split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(spec)
    except Exception:
        _log({"step": "post_at_parse_failed", "spec": spec})
        return None
    tz = ZoneInfo(tz or config.DIGEST_TZ)
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target_local.astimezone(timezone.utc)


def _wait_until_post_time(post_at: datetime) -> None:
    """Hold until `post_at` so the digest lands at exactly the scheduled minute.
    No-op if the time has passed; capped at POST_AT_MAX_WAIT_S so an over-early
    trigger doesn't park the job for hours."""
    delta = (post_at - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return
    if delta > POST_AT_MAX_WAIT_S:
        _log({"step": "post_at_skipped_too_early", "wait_seconds": round(delta)})
        _progress(
            f"      built and ready; trigger fired {round(delta / 60)} min before "
            f"the {post_at.isoformat()} post time (> {POST_AT_MAX_WAIT_S // 60} min "
            f"cap) — posting now instead of idling."
        )
        return
    _progress(
        f"      built and ready; holding {round(delta)}s until the scheduled "
        f"post time ({post_at.isoformat()})…"
    )
    _log({"step": "post_at_waiting", "wait_seconds": round(delta), "post_at": post_at.isoformat()})
    time.sleep(delta)


def run_pipeline(
    *,
    digest_date: str | None = None,
    post_at: datetime | None = None,
    force: bool = False,
    conn: sqlite3.Connection | None = None,
    chroma_client: chromadb.api.ClientAPI | None = None,
    perplexity_client: PerplexityClient | None = None,
    embedder: Embedder | None = None,
    http_client: httpx.Client | None = None,
    skip_url_validation: bool = False,
    skip_content_indexing: bool = False,
    max_plans: int | None = None,
    skip_rss: bool = False,
    dry_run: bool = False,
    test_mode: bool = False,
    geo: str = "both",
) -> PipelineStats:
    start = time.monotonic()
    target = _resolve_geo(geo)
    digest_date = digest_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    own_conn = conn is None
    if own_conn:
        conn = storage.connect()

    try:
        storage.init_db(conn=conn)
        if own_conn:
            conn.commit()

        if not skip_content_indexing:
            _progress("[0/5] Content corpus check…")
            ensure_content_indexed(chroma_client=chroma_client, embedder=embedder)

        # 1) Perplexity sweep
        plans = build_query_plans(geo=target.geo)
        if max_plans is not None:
            plans = plans[:max_plans]
        if perplexity_client is None:
            # Scope the daily call budget per geo so the India and US runs (same
            # UTC date) don't share/starve each other's cap.
            perplexity_client = PerplexityClient(
                scope="" if target.geo == "both" else target.geo,
            )
        _progress(
            f"[1/5] Perplexity sweep ({target.geo}): {len(plans)} plans  "
            f"({perplexity_client.remaining_today} of "
            f"{config.MAX_PERPLEXITY_CALLS_PER_DAY} calls remaining today)"
        )
        t0 = time.monotonic()
        if hasattr(perplexity_client, "search_recent_async"):
            perp_signals = asyncio.run(
                fetch_perplexity_async(perplexity_client, plans),
            )
        else:
            # Test path: fakes implement only the sync .search_recent().
            perp_signals = fetch_perplexity(perplexity_client, plans)
        _progress(
            f"      done: {len(perp_signals)} signals in "
            f"{time.monotonic() - t0:.1f}s  "
            f"({perplexity_client.calls_today}/"
            f"{config.MAX_PERPLEXITY_CALLS_PER_DAY} calls used)"
        )
        _log({
            "step": "perplexity_fetch_done",
            "plans_total": len(plans),
            "signals_collected": len(perp_signals),
            "calls_used_today": perplexity_client.calls_today,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        })

        # 2) RSS sweep
        t0 = time.monotonic()
        if skip_rss:
            rss_signals = []
            _progress("[2/5] RSS sweep: skipped (--skip-rss)")
            _log({"step": "rss_fetch_skipped"})
        else:
            _progress("[2/5] RSS sweep: fetching newsletters + voice feeds…")
            newsletter_signals = fetch_all_newsletters(http=http_client)
            voice_signals = fetch_all_voice_feeds(http=http_client)
            rss_signals = newsletter_signals + voice_signals
            _progress(
                f"      done: {len(rss_signals)} signals "
                f"({len(newsletter_signals)} newsletter, {len(voice_signals)} voice) "
                f"in {time.monotonic() - t0:.1f}s"
            )
            _log({
                "step": "rss_fetch_done",
                "signals_collected": len(rss_signals),
                "newsletter_signals": len(newsletter_signals),
                "voice_signals": len(voice_signals),
                "elapsed_seconds": round(time.monotonic() - t0, 2),
            })

        # 3) Persist signals
        all_signals = perp_signals + rss_signals
        n_saved = storage.save_signals(all_signals, conn=conn)
        if own_conn:
            conn.commit()
        _log({"step": "signals_saved", "n": n_saved, "input_count": len(all_signals)})

        # 4) Score & cluster
        _progress(
            f"[3/5] Scoring & dedupe: {len(all_signals)} signals "
            f"({n_saved} new)…"
        )
        t0 = time.monotonic()
        scoring_stats = scorer.run_scoring(
            conn=conn, chroma_client=chroma_client, embedder=embedder,
        )
        if own_conn:
            conn.commit()
        _progress(
            f"      done: {scoring_stats.stories_created} stories in "
            f"{time.monotonic() - t0:.1f}s"
        )
        _log({"step": "scoring_done", **asdict(scoring_stats)})

        # 5) Rank
        _progress(
            f"[4/5] Ranking: sonar-reasoning-pro "
            f"(timeout {config.HTTP_TIMEOUT_RANK_S}s)…"
        )
        ranking = ranker.rank_stories(conn=conn, client=perplexity_client)
        _progress(
            f"      done: {len(ranking.flat)} ranked in "
            f"{ranking.elapsed_seconds:.1f}s  "
            f"(fallback={ranking.used_fallback}, "
            f"cost=${ranking.cost_usd:.4f})"
        )
        _log({
            "step": "ranking_done",
            "ranked_count": len(ranking.flat),
            "top_summary_count": len(ranking.top_summary),
            "by_priority_counts": {k: len(v) for k, v in ranking.by_priority.items()},
            "other_count": len(ranking.other),
            "candidates_count": ranking.candidates_count,
            "used_fallback": ranking.used_fallback,
            "cost_usd": ranking.cost_usd,
            "elapsed_seconds": ranking.elapsed_seconds,
        })

        # 5b) Route by geo. India channel keeps India+Global, US keeps US+Global
        # (Global — incl. all AI/Hot-TA news — and unclassified RSS go to both).
        if target.allowed is not None:
            before = len(ranking.flat)
            ranking = ranker.filter_by_geo(ranking, set(target.allowed))
            _log({
                "step": "geo_filter",
                "geo": target.geo,
                "kept": len(ranking.flat),
                "dropped": before - len(ranking.flat),
            })
            _progress(
                f"      geo={target.geo}: kept {len(ranking.flat)} of {before} "
                f"stories for #{target.channel_label}"
            )

        # 6) Console
        print_digest_to_console(ranking, digest_date)

        # 7+8) Dry-run vs test vs full path
        if dry_run:
            _progress("[5/5] Dry-run: rendering blocks to disk (no Slack post)…")
            blocks = slack_client.build_blocks(
                ranking, digest_date=digest_date,
            )
            payload = {
                "text": f"Daily Healthcare Signal — {digest_date}",
                "blocks": blocks,
            }
            out = config.LOGS_DIR / f"dry_run_digest_{digest_date}.json"
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"\n[dry-run] Block Kit payload written to: {out}")
            print("[dry-run] No digest record created, no Slack post sent.")
            digest_sent = False
            _log({"step": "dry_run_complete", "payload_path": str(out)})
        elif test_mode:
            # Like a real run for Slack, but no digest row in the DB → the
            # URLs we post here won't enter the 30-day dedup window and
            # squeeze out tomorrow's real digest.
            _progress(
                f"[5/5] Test post: validating {len(ranking.flat)} URLs and "
                f"posting to #{target.channel_label} with [TEST] marker…"
            )
            slack_result = slack_client.post_digest(
                ranking,
                digest_date=digest_date,
                channel_id=target.channel_id or None,
                channel_label=target.channel_label,
                http=http_client,
                skip_url_validation=skip_url_validation,
                test_mode=True,
            )
            _progress(
                f"      done: sent={slack_result.sent}, "
                f"stories_sent={slack_result.stories_sent}, "
                f"dropped={slack_result.stories_dropped_invalid_url}  "
                f"({slack_result.elapsed_seconds:.1f}s)"
            )
            _log({
                "step": "test_post_done",
                "sent": slack_result.sent,
                "stories_sent": slack_result.stories_sent,
                "stories_dropped_invalid_url":
                    slack_result.stories_dropped_invalid_url,
                "status_code": slack_result.status_code,
                "error": slack_result.error,
                "elapsed_seconds": slack_result.elapsed_seconds,
            })
            digest_sent = slack_result.sent
        elif not force and storage.has_sent_digest_for_date(
            digest_date, slack_channel=target.channel_id or None, conn=conn,
        ):
            # This channel's digest for today already shipped (a re-trigger, or
            # the other geo already ran). Don't post twice. --force overrides.
            # Guard is per-channel, so India shipping doesn't suppress US.
            _progress(
                f"[5/5] Digest for {digest_date} → #{target.channel_label} "
                f"already sent — skipping (idempotent; use --force to re-send)."
            )
            _log({"step": "skip_already_sent", "digest_date": digest_date,
                  "geo": target.geo, "channel": target.channel_id})
            digest_sent = True
        else:
            _progress(
                f"[5/5] Slack: validating {len(ranking.flat)} URLs and posting "
                f"to #{target.channel_label}…"
            )
            digest_id = storage.create_digest(
                digest_date, (target.channel_label,),
                slack_channel=target.channel_id or None, conn=conn,
            )
            # Persistence: flat order (top → priority sections → other), rank
            # is the position in that flat list. `domain` repurposed to store
            # the priority bucket display label for audit trail.
            for rank_idx, r in enumerate(ranking.flat, start=1):
                domain_label = _priority_display(r.story.priority_bucket)
                storage.add_story_to_digest(
                    digest_id, r.story.id, rank_idx, r.one_liner, domain_label,
                    conn=conn,
                )
            if own_conn:
                conn.commit()

            # Everything is built and persisted. If a scheduled post time was
            # given, hold here until that minute so the message lands on time.
            if post_at is not None:
                _wait_until_post_time(post_at)

            slack_result = slack_client.post_digest(
                ranking,
                digest_date=digest_date,
                channel_id=target.channel_id or None,
                channel_label=target.channel_label,
                http=http_client,
                skip_url_validation=skip_url_validation,
            )
            if slack_result.sent:
                storage.mark_digest_sent(
                    digest_id,
                    slack_ts=slack_result.slack_ts,
                    slack_channel=slack_result.slack_channel,
                    conn=conn,
                )
            else:
                storage.mark_digest_failed(
                    digest_id, slack_result.error or "unknown", conn=conn,
                )
            if own_conn:
                conn.commit()
            _progress(
                f"      done: sent={slack_result.sent}, "
                f"stories_sent={slack_result.stories_sent}, "
                f"dropped={slack_result.stories_dropped_invalid_url}  "
                f"({slack_result.elapsed_seconds:.1f}s)"
            )
            _log({
                "step": "slack_done",
                "sent": slack_result.sent,
                "stories_sent": slack_result.stories_sent,
                "stories_dropped_invalid_url":
                    slack_result.stories_dropped_invalid_url,
                "status_code": slack_result.status_code,
                "error": slack_result.error,
                "elapsed_seconds": slack_result.elapsed_seconds,
            })
            digest_sent = slack_result.sent

        elapsed = round(time.monotonic() - start, 3)
        stats = PipelineStats(
            perplexity_signals=len(perp_signals),
            rss_signals=len(rss_signals),
            signals_saved=n_saved,
            stories_created=scoring_stats.stories_created,
            ranked_count=len(ranking.flat),
            digest_sent=digest_sent,
            elapsed_seconds=elapsed,
        )
        _log({"summary": True, **asdict(stats)})
        return stats
    finally:
        if own_conn and conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Daily healthcare signal pipeline.")
    p.add_argument("--max-plans", type=int, default=None,
                   help="Cap Perplexity plans (e.g. --max-plans 3 for a quick test).")
    p.add_argument("--skip-rss", action="store_true",
                   help="Skip RSS fetch (saves ~60 newsletter HTTP requests).")
    p.add_argument("--skip-content-index", action="store_true",
                   help="Skip the auto content-corpus indexing check on startup.")
    p.add_argument("--skip-url-validation", action="store_true",
                   help="Skip HEAD-validating story URLs before email.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Render the digest blocks to disk but don't persist "
                           "or post to Slack.")
    mode.add_argument("--test", action="store_true",
                      help="Run the full pipeline and post to Slack with a "
                           "[TEST] marker. Does NOT write a digest row to the "
                           "DB, so the test URLs don't enter the dedup window.")
    p.add_argument("--post-at", default=os.environ.get("DIGEST_POST_AT"),
                   help="Build everything, then hold until this HH:MM (in the "
                        "configured digest timezone) before posting, so the "
                        "message lands on time. Defaults to $DIGEST_POST_AT. "
                        "Ignored once the time has passed or it's >30 min away.")
    p.add_argument("--force", action="store_true",
                   help="Post even if today's digest was already sent "
                        "(bypasses the idempotency guard).")
    p.add_argument("--geo", default=os.environ.get("DIGEST_GEO", "both"),
                   choices=["india", "us", "both"],
                   help="Which geo to research + which channel to post. "
                        "'india' → India+Global → Signal Agent India; "
                        "'us' → US+Global → Signal Agent US; "
                        "'both' (default) → everything → single channel. "
                        "Defaults to $DIGEST_GEO.")
    args = p.parse_args(argv)

    config.check_env()
    # US posts 08:00 in America/New_York; India + legacy 'both' in DIGEST_TZ.
    post_tz = config.DIGEST_TZ_US if args.geo == "us" else config.DIGEST_TZ_INDIA
    post_at = compute_post_at(args.post_at, tz=post_tz)
    stats = run_pipeline(
        max_plans=args.max_plans,
        skip_rss=args.skip_rss,
        skip_content_indexing=args.skip_content_index,
        skip_url_validation=args.skip_url_validation,
        dry_run=args.dry_run,
        test_mode=args.test,
        post_at=post_at,
        force=args.force,
        geo=args.geo,
    )
    print()
    print(
        f"perplexity={stats.perplexity_signals}  rss={stats.rss_signals}  "
        f"saved={stats.signals_saved}  stories={stats.stories_created}  "
        f"ranked={stats.ranked_count}  sent={stats.digest_sent}  "
        f"elapsed={stats.elapsed_seconds:.1f}s"
    )
    if args.dry_run:
        return 0
    return 0 if stats.digest_sent else 1


if __name__ == "__main__":
    sys.exit(main())
