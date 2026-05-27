"""Layer 2 transport: thin wrapper around Perplexity's chat completions API.

Adds: retry, in-process + on-disk rate limiting (60/day cap), JSONL logging of
every call, and approximate cost estimation. Used by main.py for the sonar-pro
fetch sweep and by ranker.py for the sonar-reasoning ranking call. The 60/day
cap is global — counts both fetch and rank.

Returns raw text + citations; parsing the model's answer into Signal objects is
the orchestrator's job, not this module's.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

import config
from query_planner import QueryPlan

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

# Polite floor between successive sync-path calls. The async path uses an
# asyncio.Semaphore in main.py (concurrency cap) instead of a sleep floor,
# so this constant only applies when complete()/search_recent() are called
# directly (tests, ad-hoc scripts).
MIN_SECONDS_BETWEEN_CALLS = 0.5

# === Pricing — TODO: VERIFY against current Perplexity pricing.
# https://docs.perplexity.ai/guides/pricing  (placeholder values as of 2026-05-04;
# update when Perplexity changes pricing). All values in USD per 1M tokens.
SONAR_PRO_INPUT_USD_PER_MTOK = 3.0
SONAR_PRO_OUTPUT_USD_PER_MTOK = 15.0
SONAR_REASONING_INPUT_USD_PER_MTOK = 1.0
SONAR_REASONING_OUTPUT_USD_PER_MTOK = 5.0

_PRICING: dict[str, tuple[float, float]] = {
    "sonar-pro": (SONAR_PRO_INPUT_USD_PER_MTOK, SONAR_PRO_OUTPUT_USD_PER_MTOK),
    "sonar-reasoning": (
        SONAR_REASONING_INPUT_USD_PER_MTOK,
        SONAR_REASONING_OUTPUT_USD_PER_MTOK,
    ),
}

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_NETWORK = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteTimeout,
)


class RateLimitExceeded(RuntimeError):
    pass


class PerplexityCallFailed(RuntimeError):
    """Raised after retries are exhausted or on a non-retryable HTTP error."""


@dataclass(frozen=True)
class ChatResponse:
    text: str
    citations: tuple[str, ...]
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    raw: dict


# --- Helpers ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"perplexity_{_today_str()}.jsonl"


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = _PRICING.get(model)
    if pricing is None:
        return 0.0
    in_price, out_price = pricing
    return (prompt_tokens * in_price + completion_tokens * out_price) / 1_000_000.0


def _count_billable_calls_today() -> int:
    p = _log_path()
    if not p.exists():
        return 0
    n = 0
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == 200:
                n += 1
    return n


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, _RETRYABLE_NETWORK)


# --- Client -------------------------------------------------------------

class PerplexityClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        no_wait_for_tests: bool = False,
    ) -> None:
        self._api_key = api_key if api_key is not None else config.PERPLEXITY_API_KEY
        if not self._api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is not set")
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        self._http = http or httpx.Client(
            timeout=config.HTTP_TIMEOUT_S,
            headers=self._headers,
        )
        # Lazy: only created if complete_async is called.
        self._async_http: httpx.AsyncClient | None = None
        self._calls_today: int | None = None
        self._last_call_ts: float = 0.0
        self._no_wait = no_wait_for_tests

    # --- counter ---

    def _ensure_counter_loaded(self) -> None:
        if self._calls_today is None:
            self._calls_today = _count_billable_calls_today()

    @property
    def calls_today(self) -> int:
        self._ensure_counter_loaded()
        assert self._calls_today is not None
        return self._calls_today

    @property
    def remaining_today(self) -> int:
        return max(0, config.MAX_PERPLEXITY_CALLS_PER_DAY - self.calls_today)

    def _check_cap(self) -> None:
        if self.calls_today >= config.MAX_PERPLEXITY_CALLS_PER_DAY:
            raise RateLimitExceeded(
                f"Perplexity daily cap hit: {self.calls_today}/"
                f"{config.MAX_PERPLEXITY_CALLS_PER_DAY}"
            )

    def _polite_throttle(self) -> None:
        if self._no_wait:
            return
        gap = time.monotonic() - self._last_call_ts
        if gap < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - gap)
        self._last_call_ts = time.monotonic()

    # --- core call ---

    def _build_body(
        self, prompt: str, *, model: str, recency: str | None, system: str | None,
    ) -> dict:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict = {"model": model, "messages": messages}
        if recency:
            body["search_recency_filter"] = recency
        return body

    def _wait_policy(self):
        if self._no_wait:
            return wait_exponential(multiplier=0, min=0, max=0)
        return wait_exponential(multiplier=1, min=2, max=30) + wait_random(0, 1)

    def _finalize_response(
        self,
        *,
        response: httpx.Response | None,
        last_exc: BaseException | None,
        last_status: int,
        last_body: str,
        latency_ms: int,
        attempts: int,
        model: str,
        query_id: str,
    ) -> ChatResponse:
        if response is None or response.status_code != 200:
            err_str = str(last_exc) if last_exc else "no response"
            if last_body:
                err_str = f"{err_str} | body: {last_body}"
            self._log_call(
                model=model,
                query_id=query_id,
                status=last_status,
                latency_ms=latency_ms,
                prompt_tokens=0,
                completion_tokens=0,
                citations=0,
                cost_usd=0.0,
                attempts=attempts,
                error=err_str,
            )
            raise PerplexityCallFailed(
                f"Perplexity call failed (model={model}, query_id={query_id}) "
                f"after {attempts} attempt(s): HTTP {last_status} — "
                f"{last_body or last_exc}"
            ) from last_exc

        data = response.json()
        text = data["choices"][0]["message"]["content"]
        citations = tuple(data.get("citations") or [])
        usage = data.get("usage", {}) or {}
        pt = int(usage.get("prompt_tokens", 0))
        ct = int(usage.get("completion_tokens", 0))
        cost = _estimate_cost(model, pt, ct)

        self._log_call(
            model=model,
            query_id=query_id,
            status=200,
            latency_ms=latency_ms,
            prompt_tokens=pt,
            completion_tokens=ct,
            citations=len(citations),
            cost_usd=cost,
            attempts=attempts,
        )
        # Increment in-process counter (file-counter is the source of truth on next process).
        self._ensure_counter_loaded()
        self._calls_today = (self._calls_today or 0) + 1

        return ChatResponse(
            text=text,
            citations=citations,
            model=model,
            prompt_tokens=pt,
            completion_tokens=ct,
            estimated_cost_usd=cost,
            raw=data,
        )

    def complete(
        self,
        prompt: str,
        *,
        model: str = config.PERPLEXITY_MODEL_FETCH,
        recency: str | None = None,
        query_id: str = "ad-hoc",
        system: str | None = None,
        timeout: float | None = None,
    ) -> ChatResponse:
        self._check_cap()
        self._polite_throttle()
        body = self._build_body(prompt, model=model, recency=recency, system=system)

        attempts = 0
        last_exc: BaseException | None = None
        last_status = 0
        last_body = ""
        response: httpx.Response | None = None
        start = time.monotonic()

        retryer: Iterator = Retrying(
            stop=stop_after_attempt(config.HTTP_MAX_RETRIES),
            wait=self._wait_policy(),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        try:
            for attempt in retryer:
                with attempt:
                    attempts += 1
                    response = self._do_post(body, timeout=timeout)
        except httpx.HTTPStatusError as e:
            last_exc = e
            last_status = e.response.status_code
            try:
                last_body = e.response.text[:600]
            except Exception:
                last_body = ""
        except Exception as e:  # network errors after retries
            last_exc = e
            last_status = 0

        latency_ms = int((time.monotonic() - start) * 1000)
        return self._finalize_response(
            response=response, last_exc=last_exc, last_status=last_status,
            last_body=last_body, latency_ms=latency_ms, attempts=attempts,
            model=model, query_id=query_id,
        )

    async def complete_async(
        self,
        prompt: str,
        *,
        model: str = config.PERPLEXITY_MODEL_FETCH,
        recency: str | None = None,
        query_id: str = "ad-hoc",
        system: str | None = None,
        timeout: float | None = None,
    ) -> ChatResponse:
        """Async sibling of `complete`. Same cap accounting + retry semantics;
        skips the in-process polite-floor (concurrency is controlled by the
        caller via asyncio.Semaphore)."""
        self._check_cap()
        body = self._build_body(prompt, model=model, recency=recency, system=system)

        attempts = 0
        last_exc: BaseException | None = None
        last_status = 0
        last_body = ""
        response: httpx.Response | None = None
        start = time.monotonic()

        retryer = AsyncRetrying(
            stop=stop_after_attempt(config.HTTP_MAX_RETRIES),
            wait=self._wait_policy(),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        try:
            async for attempt in retryer:
                with attempt:
                    attempts += 1
                    response = await self._do_post_async(body, timeout=timeout)
        except httpx.HTTPStatusError as e:
            last_exc = e
            last_status = e.response.status_code
            try:
                last_body = e.response.text[:600]
            except Exception:
                last_body = ""
        except Exception as e:
            last_exc = e
            last_status = 0

        latency_ms = int((time.monotonic() - start) * 1000)
        return self._finalize_response(
            response=response, last_exc=last_exc, last_status=last_status,
            last_body=last_body, latency_ms=latency_ms, attempts=attempts,
            model=model, query_id=query_id,
        )

    def search_recent(self, plan: QueryPlan) -> ChatResponse:
        return self.complete(
            plan.prompt_text,
            model=config.PERPLEXITY_MODEL_FETCH,
            recency=config.PERPLEXITY_RECENCY,
            query_id=plan.id,
        )

    async def search_recent_async(self, plan: QueryPlan) -> ChatResponse:
        return await self.complete_async(
            plan.prompt_text,
            model=config.PERPLEXITY_MODEL_FETCH,
            recency=config.PERPLEXITY_RECENCY,
            query_id=plan.id,
        )

    # --- internals ---

    def _do_post(self, body: dict, *, timeout: float | None = None) -> httpx.Response:
        if timeout is not None:
            r = self._http.post(PERPLEXITY_URL, json=body, timeout=timeout)
        else:
            r = self._http.post(PERPLEXITY_URL, json=body)
        if r.status_code == 200:
            return r
        # raise_for_status() raises HTTPStatusError for any 4xx/5xx; tenacity's
        # _is_retryable() filters on status code so 429/5xx retry, others don't.
        r.raise_for_status()
        return r  # unreachable

    def _ensure_async_http(self) -> httpx.AsyncClient:
        if self._async_http is None:
            self._async_http = httpx.AsyncClient(
                timeout=config.HTTP_TIMEOUT_S,
                headers=self._headers,
            )
        return self._async_http

    async def _do_post_async(
        self, body: dict, *, timeout: float | None = None,
    ) -> httpx.Response:
        http = self._ensure_async_http()
        if timeout is not None:
            r = await http.post(PERPLEXITY_URL, json=body, timeout=timeout)
        else:
            r = await http.post(PERPLEXITY_URL, json=body)
        if r.status_code == 200:
            return r
        r.raise_for_status()
        return r  # unreachable

    async def aclose(self) -> None:
        """Close the lazily-created async client. Safe to call multiple times."""
        if self._async_http is not None:
            await self._async_http.aclose()
            self._async_http = None

    def _log_call(
        self,
        *,
        model: str,
        query_id: str,
        status: int,
        latency_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        citations: int,
        cost_usd: float,
        attempts: int,
        error: str | None = None,
    ) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "model": model,
            "query_id": query_id,
            "status": status,
            "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "citations": citations,
            "cost_usd": round(cost_usd, 6),
            "attempts": attempts,
        }
        if error:
            rec["error"] = error[:500]
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
