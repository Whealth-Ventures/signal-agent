"""Anthropic (Claude) transport for the single ranking call.

The ranker's highest-leverage, lowest-volume step — tiering candidates and
writing one-line headlines — runs once per digest. This client routes that one
call to Claude, whose editorial writing is stronger than the Perplexity reasoning
model the rest of the pipeline uses for fetching.

It mirrors PerplexityClient.complete()'s shape (same kwargs, returns the shared
ChatResponse) so ranker.py can treat the two interchangeably via its
_RankerClient protocol. `recency` is accepted-and-ignored (Claude doesn't web
search here — it only tiers the candidates we hand it).

Failures raise PerplexityCallFailed so the ranker's existing except-and-fallback
path (→ score/recency order) catches them unchanged. When ANTHROPIC_API_KEY is
unset, ranker.py never constructs this client — it falls back to Perplexity.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from perplexity_client import ChatResponse, PerplexityCallFailed

# Approximate Claude pricing, USD per 1M tokens. Update if Anthropic changes it.
# Keyed by a prefix match on the model id so aliases resolve.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    for prefix, (in_p, out_p) in _PRICING.items():
        if model.startswith(prefix):
            return (prompt_tokens * in_p + completion_tokens * out_p) / 1_000_000.0
    return 0.0


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"anthropic_{_today_str()}.jsonl"


class AnthropicClient:
    """Thin wrapper over the anthropic SDK exposing a Perplexity-compatible
    `complete()`. Constructed only when ANTHROPIC_API_KEY is set."""

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else config.ANTHROPIC_API_KEY
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # imported lazily so the dep is only needed when used
        except ImportError as e:  # pragma: no cover - environment guard
            raise RuntimeError(
                "The 'anthropic' package is required for the Claude ranker. "
                "Add it to requirements.txt / pip install anthropic."
            ) from e
        self._client = anthropic.Anthropic(api_key=self._api_key)
        self._max_tokens = config.ANTHROPIC_MAX_TOKENS_RANK

    def complete(
        self,
        prompt: str,
        *,
        model: str = "",
        recency: str | None = None,   # accepted for protocol parity; ignored
        query_id: str = "ad-hoc",
        system: str | None = None,
        timeout: float | None = None,
    ) -> ChatResponse:
        model = model or config.ANTHROPIC_MODEL_RANK
        start = time.monotonic()
        try:
            kwargs: dict = {
                "model": model,
                "max_tokens": self._max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            if timeout is not None:
                kwargs["timeout"] = timeout
            msg = self._client.messages.create(**kwargs)
        except Exception as e:
            self._log(model=model, query_id=query_id, status=0,
                      latency_ms=int((time.monotonic() - start) * 1000),
                      prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
                      error=f"{type(e).__name__}: {e}")
            raise PerplexityCallFailed(
                f"Anthropic call failed (model={model}, query_id={query_id}): "
                f"{type(e).__name__}: {e}"
            ) from e

        text = "".join(
            getattr(block, "text", "") for block in msg.content
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(msg, "usage", None)
        pt = int(getattr(usage, "input_tokens", 0) or 0)
        ct = int(getattr(usage, "output_tokens", 0) or 0)
        cost = _estimate_cost(model, pt, ct)
        self._log(model=model, query_id=query_id, status=200,
                  latency_ms=int((time.monotonic() - start) * 1000),
                  prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)

        return ChatResponse(
            text=text, citations=(), model=model,
            prompt_tokens=pt, completion_tokens=ct,
            estimated_cost_usd=cost, raw={"id": getattr(msg, "id", "")},
        )

    def _log(self, *, model: str, query_id: str, status: int, latency_ms: int,
             prompt_tokens: int, completion_tokens: int, cost_usd: float,
             error: str | None = None) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "provider": "anthropic", "model": model, "query_id": query_id,
            "status": status, "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "cost_usd": round(cost_usd, 6),
        }
        if error:
            rec["error"] = error[:500]
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
