"""src/sector_main.py — orchestrator for the weekly Sector Agent.

The third agent. Sweeps per-portfolio-company for sector/regulatory/macro/
competitor developments with material impact, groups the result by company, and
posts to the sector Slack channel. Runs from a weekly timer (Mon 08:00 IST).

Deliberately its OWN entrypoint against config.SECTOR_DB_PATH — it reuses the
daily pipeline's fetch / dedup / storage / slack transport (imported from
`main`, `scorer`, `storage`, `sector`) but never shares the daily agent's DB, so
sector stories can't leak into the daily digest's candidate pool.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import config
import scorer
import sector
import storage
from main import (
    _progress,
    _wait_until_post_time,
    compute_post_at,
    ensure_content_indexed,
    fetch_perplexity,
    fetch_perplexity_async,
)
from perplexity_client import PerplexityClient

# Sector candidate pool ceiling — with ~16 companies at a handful of stories
# each, post-dedup this is comfortably small; the cap is a sanity ceiling.
CANDIDATE_POOL_SIZE = 200


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def print_to_console(result: sector.SectorResult, digest_date: str) -> None:
    bar = "=" * 72
    print()
    print(bar)
    print(f"Sector Signal — portfolio watch — {digest_date}  ·  "
          f"{len(result.flat)} stories across {len(result.grouped)} companies")
    print(bar)
    if not result.flat:
        print("(no material sector developments this week)")
        return
    for company, items in result.grouped.items():
        print(f"\n{company} ({len(items)})")
        for it in items:
            mark = {"positive": "↑", "negative": "↓", "mixed": "↔"}.get(it.direction, "•")
            print(f"  {mark} [{it.materiality}] {it.one_liner} ({it.story.canonical_url})")
    if result.used_fallback:
        print("\n(used fallback grouping — impact ranker call failed or returned nothing)")


def run(
    *,
    digest_date: str | None = None,
    post_at: datetime | None = None,
    force: bool = False,
    skip_url_validation: bool = False,
    skip_content_indexing: bool = False,
    max_plans: int | None = None,
    dry_run: bool = False,
    test_mode: bool = False,
) -> bool:
    """Returns True if the digest was sent (or already sent / dry-run ok)."""
    start = time.monotonic()
    digest_date = digest_date or _today_str()
    channel_id = config.SLACK_CHANNEL_ID_SECTOR or None
    channel_label = config.SLACK_CHANNEL_LABEL_SECTOR

    conn = storage.connect(config.SECTOR_DB_PATH)
    try:
        storage.init_db(conn=conn)
        conn.commit()

        if not skip_content_indexing:
            _progress("[0/5] Content corpus check…")
            ensure_content_indexed()

        companies = sector.load_portfolio()
        plans = sector.build_sector_plans(companies)
        if max_plans is not None:
            plans = plans[:max_plans]

        client = PerplexityClient(scope="sector")
        _progress(
            f"[1/5] Sector sweep: {len(plans)} company plans "
            f"({client.remaining_today} of {config.MAX_PERPLEXITY_CALLS_PER_DAY} "
            f"calls remaining today), recency={config.SECTOR_RECENCY}"
        )
        t0 = time.monotonic()
        if hasattr(client, "search_recent_async"):
            perp_signals = asyncio.run(
                fetch_perplexity_async(client, plans, recency=config.SECTOR_RECENCY)
            )
        else:  # test path: fakes implement only sync search_recent
            perp_signals = fetch_perplexity(client, plans, recency=config.SECTOR_RECENCY)
        _progress(
            f"      done: {len(perp_signals)} signals in "
            f"{time.monotonic() - t0:.1f}s ({client.calls_today} calls used)"
        )

        n_saved = storage.save_signals(perp_signals, conn=conn)
        conn.commit()

        _progress(f"[2/5] Scoring & dedupe: {len(perp_signals)} signals ({n_saved} new)…")
        scoring = scorer.run_scoring(conn=conn)
        conn.commit()
        _progress(f"      done: {scoring.stories_created} stories")

        # Candidate pool: sector db's own recent stories, minus anything sent in
        # the sector channel's own dedup window. No healthcare topicality gate —
        # sector news is regulatory/macro/competitor, not necessarily "clinical".
        sent_urls = storage.recently_sent_urls(
            within_days=config.SECTOR_DEDUP_WINDOW_DAYS, conn=conn,
        )
        pool = storage.list_stories(
            min_score=0.0, limit=CANDIDATE_POOL_SIZE, exclude_urls=sent_urls,
            order_by_recency=True, conn=conn,
        )
        _progress(f"[3/5] Impact ranking: {len(pool)} candidates…")
        result = sector.rank_impact(pool, companies)
        _progress(
            f"      done: kept {len(result.flat)} across {len(result.grouped)} "
            f"companies (fallback={result.used_fallback}, cost=${result.cost_usd:.4f})"
        )

        print_to_console(result, digest_date)

        if dry_run:
            blocks = sector.build_sector_blocks(result, digest_date=digest_date)
            out = config.LOGS_DIR / f"dry_run_sector_{digest_date}.json"
            out.write_text(
                json.dumps(
                    {"text": f"Sector Signal — {digest_date}", "blocks": blocks},
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\n[dry-run] Block Kit payload written to: {out}")
            print("[dry-run] No digest record created, no Slack post sent.")
            return True

        if test_mode:
            _progress(f"[4/5] Test post → #{channel_label} with [TEST] marker…")
            res = sector.post_sector_digest(
                result, digest_date=digest_date, channel_id=channel_id,
                channel_label=channel_label, skip_url_validation=skip_url_validation,
                test_mode=True,
            )
            _progress(f"      done: sent={res.sent}, stories_sent={res.stories_sent}, "
                      f"dropped={res.stories_dropped_invalid_url}")
            return res.sent

        if not force and storage.has_sent_digest_for_date(
            digest_date, slack_channel=channel_id, conn=conn,
        ):
            _progress(f"[4/5] Sector digest for {digest_date} → #{channel_label} "
                      f"already sent — skipping (use --force to re-send).")
            return True

        _progress(f"[4/5] Posting {len(result.flat)} stories → #{channel_label}…")
        digest_id = storage.create_digest(
            digest_date, (channel_label,), slack_channel=channel_id, conn=conn,
        )
        for rank_idx, it in enumerate(result.flat, start=1):
            storage.add_story_to_digest(
                digest_id, it.story.id, rank_idx, it.one_liner, it.company, conn=conn,
            )
        conn.commit()

        if post_at is not None:
            _wait_until_post_time(post_at)

        res = sector.post_sector_digest(
            result, digest_date=digest_date, channel_id=channel_id,
            channel_label=channel_label, skip_url_validation=skip_url_validation,
        )
        if res.sent:
            storage.mark_digest_sent(
                digest_id, slack_ts=res.slack_ts,
                slack_channel=res.slack_channel or channel_id, conn=conn,
            )
        else:
            storage.mark_digest_failed(digest_id, res.error or "unknown", conn=conn)
        conn.commit()
        _progress(
            f"[5/5] done: sent={res.sent}, stories_sent={res.stories_sent}, "
            f"dropped={res.stories_dropped_invalid_url} "
            f"({time.monotonic() - start:.1f}s total)"
        )
        return res.sent
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Weekly Sector Agent (portfolio impact digest).")
    p.add_argument("--max-plans", type=int, default=None,
                   help="Cap company plans (e.g. --max-plans 2 for a quick test).")
    p.add_argument("--skip-content-index", action="store_true")
    p.add_argument("--skip-url-validation", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Render blocks to disk; no DB record, no Slack post.")
    mode.add_argument("--test", action="store_true",
                      help="Post to Slack with a [TEST] marker; no DB digest row.")
    p.add_argument("--post-at", default=os.environ.get("DIGEST_POST_AT"),
                   help="Hold until this HH:MM (India tz) before posting.")
    p.add_argument("--force", action="store_true",
                   help="Post even if this week's sector digest already went out.")
    args = p.parse_args(argv)

    config.check_env()
    if not config.PORTFOLIO_XLSX.exists():
        raise RuntimeError(
            f"Portfolio file not found: {config.PORTFOLIO_XLSX}. Run "
            f"`python scripts/build_portfolio_xlsx.py` to bootstrap it."
        )
    post_at = compute_post_at(args.post_at, tz=config.DIGEST_TZ_INDIA)
    sent = run(
        max_plans=args.max_plans,
        skip_content_indexing=args.skip_content_index,
        skip_url_validation=args.skip_url_validation,
        dry_run=args.dry_run,
        test_mode=args.test,
        post_at=post_at,
        force=args.force,
    )
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
