"""Shared data types that flow between layers (fetchers → scorer → ranker → slack_client).

Per-module loader types (Voice, Newsletter, KeywordRow, QueryPlan, ChatResponse,
etc.) stay with their producer modules. This module is for types that cross
module boundaries.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SourceType = Literal["rss", "perplexity"]


@dataclass(frozen=True)
class Signal:
    """A single raw news item from any fetcher.

    `published_at` must be timezone-aware (UTC). `raw` retains the original
    fetcher payload for debugging / future schema evolution; nothing downstream
    should rely on its shape.
    """
    source: str
    source_type: SourceType
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Story:
    """A deduped/clustered story — possibly aggregating multiple Signals.

    `signal_ids` is the set of Signal ids (from `signal_id()`) that this story
    represents. `relevance_score` is set by scorer.py. `priority_bucket` is the
    PriorityBucket.key (from config) the story rolls up to, or None if the story
    came from a Track B / voice / RSS source that doesn't map cleanly to one of
    the 8 priority categories — those are grouped as "Other" at the bottom of
    the Slack digest.
    """
    id: str
    canonical_url: str
    canonical_title: str
    canonical_summary: str
    published_at: datetime
    relevance_score: float
    signal_ids: tuple[str, ...] = ()
    priority_bucket: str | None = None
    geo: str | None = None       # "India", "US", "Global", or None (RSS / unknown)
    # Top-level keyword bucket (Master Keywords "Bucket" column) the story rolls
    # up to, e.g. "Funding & Deals". Used to sub-categorize the "Other healthcare
    # news" section in the Slack digest. None for sources with no plan-derived
    # bucket (e.g. RSS-only clusters).
    bucket: str | None = None


def signal_id(source: str, url: str) -> str:
    """Deterministic id for a signal — same (source, url) → same id."""
    return hashlib.sha256(f"{source}|{url}".encode("utf-8")).hexdigest()[:16]


def story_id(canonical_url: str) -> str:
    """Deterministic id for a story — same canonical_url → same id."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
