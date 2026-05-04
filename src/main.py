"""src/main.py — orchestrator. Run from cron at 10am IST.

Wires the daily pipeline together: setup → fetch (Perplexity + RSS) → save →
score → rank → console + email → persist digest. Returns exit code 0 if the
email was sent, 1 otherwise.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import chromadb
import httpx

import config
import emailer
import ranker
import scorer
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
from rss_fetcher import fetch_all_newsletters

PERPLEXITY_HEADROOM = 2  # leave room for ranker (1 call) + 1 retry/buffer


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
                raw={"plan_id": plan.id, "bucket": plan.bucket},
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
            raw={"plan_id": plan.id, "fallback": True},
        ))
    return out


def fetch_perplexity(
    client: PerplexityClient,
    plans: list[QueryPlan],
    *,
    headroom: int = PERPLEXITY_HEADROOM,
) -> list[Signal]:
    """Iterate plans; collect Signals. Per-plan failures are logged + skipped.

    Stops early when remaining_today drops to `headroom` so the ranker call
    later in the pipeline still has budget.
    """
    out: list[Signal] = []
    for plan in plans:
        if client.remaining_today <= headroom:
            _log({
                "step": "perplexity_fetch_capped",
                "remaining": client.remaining_today,
                "headroom": headroom,
                "skipped_plan": plan.id,
            })
            break
        try:
            resp = client.search_recent(plan)
            out.extend(parse_perplexity_response(plan, resp))
        except RateLimitExceeded as e:
            _log({"step": "perplexity_fetch_rate_limit",
                  "plan": plan.id, "error": str(e)})
            break
        except Exception as e:
            _log({"step": "perplexity_fetch_plan_failed",
                  "plan": plan.id, "error": f"{type(e).__name__}: {e}"})
            continue
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


def print_top_5_to_console(ranking: ranker.RankingResult, digest_date: str) -> None:
    bar = "=" * 72
    print()
    print(bar)
    print(f"Daily Healthcare Signal — {digest_date}")
    print(bar)
    if not ranking.ranked:
        print("(no stories qualified today)")
    for r in ranking.ranked:
        host = urlparse(r.story.canonical_url).netloc or r.story.canonical_url
        print(f"\n#{r.rank}  {host}")
        print(f"    {r.story.canonical_title}")
        print(f"    {r.story.canonical_url}")
        if r.reasoning:
            print(f"    Why: {r.reasoning}")
    print()
    if ranking.used_fallback:
        print("(used score-based fallback for some slots)")


# --- Pipeline -----------------------------------------------------------

def run_pipeline(
    *,
    digest_date: str | None = None,
    conn: sqlite3.Connection | None = None,
    chroma_client: chromadb.api.ClientAPI | None = None,
    perplexity_client: PerplexityClient | None = None,
    smtp_factory: Callable | None = None,
    embedder: Embedder | None = None,
    http_client: httpx.Client | None = None,
    skip_url_validation: bool = False,
    skip_content_indexing: bool = False,
) -> PipelineStats:
    start = time.monotonic()
    digest_date = digest_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    own_conn = conn is None
    if own_conn:
        conn = storage.connect()

    try:
        storage.init_db(conn=conn)
        if own_conn:
            conn.commit()

        if not skip_content_indexing:
            ensure_content_indexed(chroma_client=chroma_client, embedder=embedder)

        # 1) Perplexity sweep
        plans = build_query_plans()
        if perplexity_client is None:
            perplexity_client = PerplexityClient()
        t0 = time.monotonic()
        perp_signals = fetch_perplexity(perplexity_client, plans)
        _log({
            "step": "perplexity_fetch_done",
            "plans_total": len(plans),
            "signals_collected": len(perp_signals),
            "calls_used_today": perplexity_client.calls_today,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        })

        # 2) RSS sweep
        t0 = time.monotonic()
        rss_signals = fetch_all_newsletters(http=http_client)
        _log({
            "step": "rss_fetch_done",
            "signals_collected": len(rss_signals),
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        })

        # 3) Persist signals
        all_signals = perp_signals + rss_signals
        n_saved = storage.save_signals(all_signals, conn=conn)
        if own_conn:
            conn.commit()
        _log({"step": "signals_saved", "n": n_saved, "input_count": len(all_signals)})

        # 4) Score & cluster
        scoring_stats = scorer.run_scoring(
            conn=conn, chroma_client=chroma_client, embedder=embedder,
        )
        if own_conn:
            conn.commit()
        _log({"step": "scoring_done", **asdict(scoring_stats)})

        # 5) Rank
        ranking = ranker.rank_stories(conn=conn, client=perplexity_client)
        _log({
            "step": "ranking_done",
            "ranked_count": len(ranking.ranked),
            "candidates_count": ranking.candidates_count,
            "used_fallback": ranking.used_fallback,
            "cost_usd": ranking.cost_usd,
            "elapsed_seconds": ranking.elapsed_seconds,
        })

        # 6) Console
        print_top_5_to_console(ranking, digest_date)

        # 7) Persist digest header + story rows
        digest_id = storage.create_digest(
            digest_date, config.DIGEST_RECIPIENTS, conn=conn,
        )
        for r in ranking.ranked:
            storage.add_story_to_digest(
                digest_id, r.story.id, r.rank, r.reasoning, conn=conn,
            )
        if own_conn:
            conn.commit()

        # 8) Email
        email_result = emailer.send_digest(
            ranking.ranked,
            digest_date=digest_date,
            smtp_factory=smtp_factory,
            http=http_client,
            skip_url_validation=skip_url_validation,
        )
        if email_result.sent:
            storage.mark_digest_sent(digest_id, conn=conn)
        else:
            storage.mark_digest_failed(
                digest_id, email_result.error or "unknown", conn=conn,
            )
        if own_conn:
            conn.commit()
        _log({
            "step": "email_done",
            "sent": email_result.sent,
            "stories_sent": email_result.stories_sent,
            "stories_dropped_invalid_url": email_result.stories_dropped_invalid_url,
            "error": email_result.error,
            "elapsed_seconds": email_result.elapsed_seconds,
        })

        elapsed = round(time.monotonic() - start, 3)
        stats = PipelineStats(
            perplexity_signals=len(perp_signals),
            rss_signals=len(rss_signals),
            signals_saved=n_saved,
            stories_created=scoring_stats.stories_created,
            ranked_count=len(ranking.ranked),
            digest_sent=email_result.sent,
            elapsed_seconds=elapsed,
        )
        _log({"summary": True, **asdict(stats)})
        return stats
    finally:
        if own_conn and conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    config.check_env()
    stats = run_pipeline()
    print()
    print(
        f"perplexity={stats.perplexity_signals}  rss={stats.rss_signals}  "
        f"saved={stats.signals_saved}  stories={stats.stories_created}  "
        f"ranked={stats.ranked_count}  sent={stats.digest_sent}  "
        f"elapsed={stats.elapsed_seconds:.1f}s"
    )
    return 0 if stats.digest_sent else 1


if __name__ == "__main__":
    sys.exit(main())
