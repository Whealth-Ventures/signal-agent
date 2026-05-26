"""Layer 3: dedupe + relevance scoring.

Pulls unscored signals from storage, drops URLs sent in the last 7 days,
clusters near-duplicates by embedding cosine similarity, scores each cluster
against the firm's content corpus + deterministic boosters, then upserts
Stories and links signals to them.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import chromadb

import config
import storage
from content_indexer import Embedder, ScoredChunk, query_similar
from models import Signal, Story, signal_id, story_id
from query_planner import load_firm_additions, load_newsletters, load_voices

SIMILARITY_THRESHOLD = 0.85
TOP_K_FOR_CONTENT_SIMILARITY = 5
SUMMARY_TRUNCATE_FOR_EMBED = 400
TRUSTED_PUBLICATION_BOOST = 0.08
FIRM_MENTION_BOOST = 0.08

# Cross-day dedup. Looser than the within-day cluster threshold because
# different outlets covering the same event use different wording.
HISTORICAL_DEDUP_THRESHOLD = 0.80
HISTORICAL_DEDUP_WINDOW_DAYS = 30
URL_DEDUP_WINDOW_DAYS = 30

# Booster table — tunable in one place. The "tier1_voice", "trusted_publication",
# and "firm_mention" entries are special-cased (matched against sets, not regex).
BOOSTERS: dict[str, tuple[float, re.Pattern | None]] = {
    "tier1_voice":         (0.10, None),
    "trusted_publication": (TRUSTED_PUBLICATION_BOOST, None),
    "firm_mention":        (FIRM_MENTION_BOOST, None),
    "funding":    (0.05, re.compile(r"\b(raises?|series [a-d]|seed round|funding)\b", re.IGNORECASE)),
    "m_and_a":    (0.05, re.compile(r"\b(acquires?|acquisition|merges? with|m&a)\b", re.IGNORECASE)),
    "regulatory": (0.05, re.compile(r"\b(fda|cdsco|ema|approved|cleared)\b", re.IGNORECASE)),
    "product":    (0.03, re.compile(r"\b(launches?|unveils?|debuts?)\b", re.IGNORECASE)),
    "leadership": (0.03, re.compile(r"\b(appoints?|named|joins|hires?)\b", re.IGNORECASE)),
    "listicle":   (-0.10, re.compile(r"^\s*\d+\s+(best|top|essential|reasons|tips|ways)\b", re.IGNORECASE)),
    "opinion":    (-0.05, re.compile(r"^\s*(opinion|perspective|column):", re.IGNORECASE)),
}


@dataclass(frozen=True)
class ScoreBreakdown:
    content_similarity: float
    booster_total: float
    boosters: dict[str, float]
    final: float


@dataclass(frozen=True)
class ScoringStats:
    signals_in: int
    signals_filtered_recent: int
    clusters_filtered_historical: int
    stories_created: int
    elapsed_seconds: float


# --- Helpers ------------------------------------------------------------

def _signal_text(s: Signal) -> str:
    summary = (s.summary or "")[:SUMMARY_TRUNCATE_FOR_EMBED]
    return f"{s.title} — {summary}".strip()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _max_similarity(
    embedding: list[float],
    historical: list[tuple[str, list[float]]],
) -> tuple[float, str | None]:
    best_sim = 0.0
    best_id: str | None = None
    for sid, vec in historical:
        sim = _cosine(embedding, vec)
        if sim > best_sim:
            best_sim = sim
            best_id = sid
    return best_sim, best_id


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    if n == 0:
        return []
    dim = len(vecs[0])
    out = [0.0] * dim
    for v in vecs:
        for i, x in enumerate(v):
            out[i] += x
    return [x / n for x in out]


def _tier1_voice_names() -> set[str]:
    return {v.name for v in load_voices() if v.tier == 1 and v.name}


def _firm_names() -> set[str]:
    """Firm names from the `New Additions` tab — used by the firm_mention
    booster to surface stories involving PE/VC firms we explicitly track."""
    return {f.firm for f in load_firm_additions() if f.firm}


def _normalize_host(host: str) -> str:
    h = host.lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def _trusted_publication_hosts() -> set[str]:
    """Hostnames of newsletters/publications curated in voices.xlsx.

    Story URLs whose host matches (or is a subdomain of) one of these gets a
    boost — operationalizes the "trusted sources first" feedback.
    """
    hosts: set[str] = set()
    for nl in load_newsletters():
        if not nl.url:
            continue
        try:
            host = urlparse(nl.url).netloc
        except Exception:
            continue
        if host:
            hosts.add(_normalize_host(host))
    return hosts


def _matches_trusted_host(url: str, trusted: set[str]) -> bool:
    if not trusted:
        return False
    try:
        host = _normalize_host(urlparse(url).netloc)
    except Exception:
        return False
    if not host:
        return False
    if host in trusted:
        return True
    # Subdomain match: e.g. newsletter publishes at substack.com/p/...
    # and a story comes in via author.substack.com.
    return any(host.endswith("." + t) for t in trusted)


# --- Pure functions -----------------------------------------------------

def cluster_signals(
    embeddings: list[list[float]],
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Greedy clustering: returns list of clusters, each a list of indices.

    A signal joins the existing cluster whose centroid has the highest cosine
    similarity above threshold; otherwise it starts a new cluster. Centroid is
    a running mean of member vectors.
    """
    clusters: list[dict] = []
    for i, emb in enumerate(embeddings):
        best_j = -1
        best_sim = threshold
        for j, c in enumerate(clusters):
            sim = _cosine(emb, c["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_j = j
        if best_j == -1:
            clusters.append({"members": [i], "vecs": [emb], "centroid": list(emb)})
        else:
            c = clusters[best_j]
            c["members"].append(i)
            c["vecs"].append(emb)
            c["centroid"] = _mean_vec(c["vecs"])
    return [c["members"] for c in clusters]


def _source_tier_rank(url: str) -> int:
    """Position of the URL's host in config.SOURCE_TIER_1 (lower = better).
    Returns len(SOURCE_TIER_1) for hosts not in the list — pushes them last."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return len(config.SOURCE_TIER_1)
    if host.startswith("www."):
        host = host[4:]
    for i, tier1 in enumerate(config.SOURCE_TIER_1):
        if host == tier1 or host.endswith("." + tier1):
            return i
    return len(config.SOURCE_TIER_1)


def pick_canonical_idx(signals: list[Signal]) -> int:
    """Source-tier primary, then longest summary, then earliest published_at,
    then (source, title) for stability. Tier-1 source list lives in
    config.SOURCE_TIER_1 — when dedupe collapses N URLs, the Tier-1 outlet
    wins the canonical slot so the Slack link points to the strongest source."""
    if not signals:
        raise ValueError("pick_canonical_idx: empty list")
    return min(
        range(len(signals)),
        key=lambda i: (
            _source_tier_rank(signals[i].url),
            -len(signals[i].summary or ""),
            signals[i].published_at,
            signals[i].source,
            signals[i].title,
        ),
    )


def pick_canonical(signals: list[Signal]) -> Signal:
    return signals[pick_canonical_idx(signals)]


def _most_common_raw_field(signals: list[Signal], field: str) -> str | None:
    """Across a cluster of signals, return the most common non-empty value of
    `signal.raw[field]`. Tie-break: first-seen order."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for s in signals:
        v = (s.raw or {}).get(field)
        if not v:
            continue
        if v not in counts:
            order.append(v)
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(order, key=lambda k: (counts[k], -order.index(k)))


def pick_priority_bucket(signals: list[Signal]) -> str | None:
    """The most common priority_bucket among contributing signals, or None if
    no signal in the cluster came from a Track A plan."""
    return _most_common_raw_field(signals, "priority_bucket")


def pick_geo(signals: list[Signal]) -> str | None:
    """The most common geography ('India' / 'US' / 'Global') among contributing
    signals. Returns None for RSS-only clusters (no plan-derived geo)."""
    return _most_common_raw_field(signals, "geo")


def compute_boosters(
    signal: Signal,
    tier1_voice_names: set[str],
    trusted_hosts: set[str] | None = None,
    firm_names: set[str] | None = None,
) -> dict[str, float]:
    out: dict[str, float] = {}
    text = f"{signal.title} {signal.summary or ''}"
    trusted_hosts = trusted_hosts or set()
    firm_names = firm_names or set()
    for name, (delta, pattern) in BOOSTERS.items():
        if name == "tier1_voice":
            for v in tier1_voice_names:
                if v and v in text:
                    out[name] = delta
                    break
        elif name == "trusted_publication":
            if _matches_trusted_host(signal.url, trusted_hosts):
                out[name] = delta
        elif name == "firm_mention":
            for f in firm_names:
                if f and f in text:
                    out[name] = delta
                    break
        else:
            if pattern is not None and pattern.search(text):
                out[name] = delta
    return out


def score_story(
    content_chunks: list[ScoredChunk],
    boosters: dict[str, float],
) -> ScoreBreakdown:
    if not content_chunks:
        content_sim = 0.0
    else:
        top = content_chunks[:TOP_K_FOR_CONTENT_SIMILARITY]
        # Chroma's cosine distance is 1 - cosine_similarity for normalized vecs.
        sims = [max(0.0, 1.0 - c.distance) for c in top]
        content_sim = sum(sims) / len(sims)
    booster_total = sum(boosters.values())
    final = max(0.0, min(1.0, content_sim + booster_total))
    return ScoreBreakdown(
        content_similarity=round(content_sim, 4),
        booster_total=round(booster_total, 4),
        boosters=dict(boosters),
        final=round(final, 4),
    )


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"scorer_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# --- Orchestrator -------------------------------------------------------

def run_scoring(
    *,
    conn: sqlite3.Connection | None = None,
    chroma_client: chromadb.api.ClientAPI | None = None,
    embedder: Embedder | None = None,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    historical_dedup_threshold: float = HISTORICAL_DEDUP_THRESHOLD,
    historical_dedup_window_days: int = HISTORICAL_DEDUP_WINDOW_DAYS,
    url_dedup_window_days: int = URL_DEDUP_WINDOW_DAYS,
    voice_names: set[str] | None = None,
    trusted_hosts: set[str] | None = None,
    firm_names: set[str] | None = None,
) -> ScoringStats:
    start = time.monotonic()
    own_conn = conn is None
    if own_conn:
        conn = storage.connect()

    try:
        signals_all = storage.list_unscored_signals(conn=conn)
        sent_urls = storage.recently_sent_urls(
            within_days=url_dedup_window_days, conn=conn,
        )
        signals = [s for s in signals_all if s.url not in sent_urls]
        filtered = len(signals_all) - len(signals)

        if not signals:
            stats = ScoringStats(
                signals_in=len(signals_all),
                signals_filtered_recent=filtered,
                clusters_filtered_historical=0,
                stories_created=0,
                elapsed_seconds=round(time.monotonic() - start, 3),
            )
            _log({"summary": True, **stats.__dict__})
            if own_conn:
                conn.commit()
            return stats

        signals.sort(key=lambda s: (-s.published_at.timestamp(), s.source, s.title))

        if embedder is None:
            from content_indexer import _make_openai_embedder
            embedder, _calls = _make_openai_embedder(api_key=config.OPENAI_API_KEY)

        texts = [_signal_text(s) for s in signals]
        embeddings, _tokens = embedder(texts)

        clusters = cluster_signals(embeddings, threshold=similarity_threshold)
        names = voice_names if voice_names is not None else _tier1_voice_names()
        hosts = (
            trusted_hosts if trusted_hosts is not None
            else _trusted_publication_hosts()
        )
        firms = firm_names if firm_names is not None else _firm_names()

        # Load historical embeddings once. Empty list on first ever run.
        historical = storage.recent_story_embeddings(
            within_days=historical_dedup_window_days, conn=conn,
        )

        stories_created = 0
        clusters_filtered_historical = 0
        for cluster in clusters:
            cs = [signals[i] for i in cluster]
            local_idx = pick_canonical_idx(cs)
            canonical = cs[local_idx]
            canonical_emb = embeddings[cluster[local_idx]]

            # Cross-day dedup: if any recently-sent story has a near-identical
            # embedding, drop this cluster before paying tokens to rank it.
            best_sim, best_id = _max_similarity(canonical_emb, historical)
            if best_sim >= historical_dedup_threshold:
                clusters_filtered_historical += 1
                _log({
                    "step": "cluster_filtered_historical_dup",
                    "canonical_url": canonical.url,
                    "matched_story_id": best_id,
                    "similarity": round(best_sim, 4),
                    "threshold": historical_dedup_threshold,
                    "cluster_size": len(cs),
                })
                continue

            content_chunks = query_similar(
                k=TOP_K_FOR_CONTENT_SIMILARITY * 2,
                embedding=canonical_emb,
                chroma_client=chroma_client,
                embedder=embedder,
            )
            boosters = compute_boosters(canonical, names, hosts, firms)
            breakdown = score_story(content_chunks, boosters)
            priority_bucket = pick_priority_bucket(cs)
            geo = pick_geo(cs)

            sid = story_id(canonical.url)
            story = Story(
                id=sid,
                canonical_url=canonical.url,
                canonical_title=canonical.title,
                canonical_summary=canonical.summary,
                published_at=canonical.published_at,
                relevance_score=breakdown.final,
                signal_ids=tuple(signal_id(s.source, s.url) for s in cs),
                priority_bucket=priority_bucket,
                geo=geo,
            )
            storage.upsert_story(story, embedding=canonical_emb, conn=conn)
            for s in cs:
                storage.assign_signal_to_story(
                    signal_id(s.source, s.url), sid, conn=conn,
                )

            # Newly-stored embedding becomes available for the rest of this run,
            # so two clusters from today's batch can't both survive if they'd
            # match each other across days. (Within-day dedup is the clustering
            # step above, but it uses a tighter threshold.)
            historical.append((sid, canonical_emb))

            stories_created += 1
            _log({
                "story_id": sid,
                "canonical_url": canonical.url,
                "cluster_size": len(cs),
                "priority_bucket": priority_bucket,
                "geo": geo,
                "content_similarity": breakdown.content_similarity,
                "boosters": breakdown.boosters,
                "booster_total": breakdown.booster_total,
                "final_score": breakdown.final,
            })

        if own_conn:
            conn.commit()

        stats = ScoringStats(
            signals_in=len(signals_all),
            signals_filtered_recent=filtered,
            clusters_filtered_historical=clusters_filtered_historical,
            stories_created=stories_created,
            elapsed_seconds=round(time.monotonic() - start, 3),
        )
        _log({"summary": True, **stats.__dict__})
        return stats
    finally:
        if own_conn and conn is not None:
            conn.close()
