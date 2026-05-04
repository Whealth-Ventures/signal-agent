"""Shared data types that flow between layers (fetchers → scorer → ranker → emailer).

Per-module loader types (Voice, Newsletter, KeywordRow, QueryPlan, ChatResponse,
etc.) stay with their producer modules. This module is for types that cross
module boundaries.
"""
from __future__ import annotations

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
