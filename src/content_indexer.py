"""Layer 3 prerequisite (offline): embed content/ into a local Chroma DB.

The firm's published content (markdown files in content/) is the "taste profile"
used by scorer.py to compute relevance — cosine similarity vs incoming stories.

Idempotent: each chunk's metadata stores the SHA-256 of its source file. On
reindex, files whose hash already matches in Chroma are skipped. `--force`
drops and rebuilds the whole collection.

CLI: `python src/content_indexer.py [--force]`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import chromadb
from openai import OpenAI
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

import config

COLLECTION_NAME = "content_corpus"
TARGET_CHUNK_WORDS = 500
MAX_CHUNK_TOKENS = 8000          # under text-embedding-3-small's 8192 limit
APPROX_CHARS_PER_TOKEN = 4

# === Pricing — TODO: VERIFY at https://platform.openai.com/docs/pricing ===
# text-embedding-3-small as of 2026-05-04. USD per 1M tokens.
EMBEDDING_USD_PER_MTOK = 0.02

Embedder = Callable[[list[str]], tuple[list[list[float]], int]]
"""Takes texts, returns (embeddings, total_tokens_consumed)."""


@dataclass(frozen=True)
class IndexStats:
    files_total: int
    files_embedded: int
    files_skipped: int
    chunks_embedded: int
    embedding_calls: int
    estimated_cost_usd: float
    elapsed_seconds: float


@dataclass(frozen=True)
class ScoredChunk:
    file_path: str
    subfolder: str
    chunk_index: int
    text: str
    distance: float          # cosine distance; lower = more similar


# --- Chunking -----------------------------------------------------------

def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def _extract_h1(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
        if line:
            return ""        # first non-blank line is not an H1
    return ""


def _truncate_to_token_budget(text: str) -> str:
    max_chars = MAX_CHUNK_TOKENS * APPROX_CHARS_PER_TOKEN
    return text[:max_chars] if len(text) > max_chars else text


def chunk_markdown(text: str, *, target_words: int = TARGET_CHUNK_WORDS) -> list[str]:
    body = _strip_frontmatter(text)
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_words = 0
    for p in paragraphs:
        p_words = len(p.split())
        if buf and buf_words + p_words > target_words:
            chunks.append("\n\n".join(buf))
            buf, buf_words = [p], p_words
        else:
            buf.append(p)
            buf_words += p_words
    if buf:
        chunks.append("\n\n".join(buf))
    return [_truncate_to_token_budget(c) for c in chunks]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Embedder factory ---------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    # OpenAI SDK raises specific exception types; treat anything looking like
    # a transient/server failure as retryable.
    name = type(exc).__name__
    if name in {"RateLimitError", "APIConnectionError", "APITimeoutError",
                "InternalServerError", "APIStatusError"}:
        # APIStatusError covers 5xx; 4xx subclasses (BadRequestError, etc.) won't
        # match by name and so won't retry.
        if name == "APIStatusError":
            status = getattr(exc, "status_code", 0)
            return status >= 500
        return True
    return False


def _make_openai_embedder(*, api_key: str, batch_size: int = 64) -> tuple[Embedder, list[int]]:
    """Returns (embedder_fn, call_counter_list_of_one).

    The counter list is a tiny hack so the caller can read .call_count after
    the embedder has been used (closures can't be inspected otherwise).
    """
    client = OpenAI(api_key=api_key)
    call_count = [0]

    def embed_batch(texts: list[str]) -> tuple[list[list[float]], int]:
        retryer = Retrying(
            stop=stop_after_attempt(config.HTTP_MAX_RETRIES),
            wait=wait_exponential(multiplier=1, min=2, max=30) + wait_random(0, 1),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        for attempt in retryer:
            with attempt:
                call_count[0] += 1
                resp = client.embeddings.create(
                    model=config.EMBEDDING_MODEL,
                    input=texts,
                )
        return ([d.embedding for d in resp.data],
                resp.usage.prompt_tokens if resp.usage else 0)

    def embedder(texts: list[str]) -> tuple[list[list[float]], int]:
        all_embs: list[list[float]] = []
        total_tokens = 0
        for i in range(0, len(texts), batch_size):
            embs, toks = embed_batch(texts[i:i + batch_size])
            all_embs.extend(embs)
            total_tokens += toks
        return all_embs, total_tokens

    return embedder, call_count


# --- Logging ------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log_path() -> Path:
    return config.LOGS_DIR / f"content_indexer_{_today_str()}.jsonl"


def _log(rec: dict) -> None:
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# --- Chroma helpers -----------------------------------------------------

def _default_chroma_client() -> chromadb.api.ClientAPI:
    return chromadb.PersistentClient(path=str(config.VECTOR_STORE_DIR))


def _get_or_create_collection(client: chromadb.api.ClientAPI):
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _existing_hash_for_file(collection, file_path: str) -> str | None:
    res = collection.get(where={"file_path": file_path}, include=["metadatas"])
    metas = res.get("metadatas") or []
    for m in metas:
        h = m.get("file_hash")
        if h:
            return h
    return None


def _delete_chunks_for_file(collection, file_path: str) -> None:
    collection.delete(where={"file_path": file_path})


# --- Public API ---------------------------------------------------------

def reindex_content_dir(
    *,
    force: bool = False,
    batch_size: int = 64,
    content_dir: Path | None = None,
    chroma_client: chromadb.api.ClientAPI | None = None,
    embedder: Embedder | None = None,
    embedding_call_count: list[int] | None = None,
) -> IndexStats:
    start = time.monotonic()
    cdir = content_dir or config.CONTENT_DIR
    client = chroma_client or _default_chroma_client()

    if force:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
    collection = _get_or_create_collection(client)

    if embedder is None:
        embedder, embedding_call_count = _make_openai_embedder(
            api_key=config.OPENAI_API_KEY,
            batch_size=batch_size,
        )
    elif embedding_call_count is None:
        embedding_call_count = [0]

    files = sorted(cdir.rglob("*.md"))
    stats = {
        "files_total": len(files),
        "files_embedded": 0,
        "files_skipped": 0,
        "chunks_embedded": 0,
        "tokens_total": 0,
    }

    for file_path in files:
        rel = file_path.relative_to(cdir)
        subfolder = rel.parts[0] if len(rel.parts) > 1 else ""
        rel_str = str(rel)
        t0 = time.monotonic()
        try:
            current_hash = file_sha256(file_path)
            existing_hash = _existing_hash_for_file(collection, rel_str)
            if existing_hash == current_hash and not force:
                stats["files_skipped"] += 1
                _log({
                    "file_path": rel_str, "subfolder": subfolder,
                    "action": "skipped_hash_match", "chunks": 0,
                    "file_hash": current_hash, "cost_usd": 0.0,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
                continue

            text = file_path.read_text(encoding="utf-8")
            title = _extract_h1(text)
            chunks = chunk_markdown(text)
            if not chunks:
                _log({
                    "file_path": rel_str, "subfolder": subfolder,
                    "action": "skipped_empty", "chunks": 0,
                    "file_hash": current_hash, "cost_usd": 0.0,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
                continue

            action = "embedded"
            if existing_hash is not None and not force:
                _delete_chunks_for_file(collection, rel_str)
                action = "deleted_and_reembedded"

            embeddings, tokens = embedder(chunks)
            stats["tokens_total"] += tokens

            ids = [f"{rel_str}::{i}" for i in range(len(chunks))]
            metadatas: list[dict[str, Any]] = [{
                "file_path": rel_str,
                "subfolder": subfolder,
                "file_hash": current_hash,
                "chunk_index": i,
                "chunk_total": len(chunks),
                "title": title,
            } for i in range(len(chunks))]

            collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=chunks,
                metadatas=metadatas,
            )

            stats["files_embedded"] += 1
            stats["chunks_embedded"] += len(chunks)
            cost = tokens * EMBEDDING_USD_PER_MTOK / 1_000_000.0
            _log({
                "file_path": rel_str, "subfolder": subfolder,
                "action": action, "chunks": len(chunks),
                "file_hash": current_hash, "tokens": tokens,
                "cost_usd": round(cost, 6),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            })
        except Exception as e:
            _log({
                "file_path": rel_str, "subfolder": subfolder,
                "action": "error", "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            })

    elapsed = time.monotonic() - start
    cost = stats["tokens_total"] * EMBEDDING_USD_PER_MTOK / 1_000_000.0
    summary = IndexStats(
        files_total=stats["files_total"],
        files_embedded=stats["files_embedded"],
        files_skipped=stats["files_skipped"],
        chunks_embedded=stats["chunks_embedded"],
        embedding_calls=embedding_call_count[0] if embedding_call_count else 0,
        estimated_cost_usd=round(cost, 6),
        elapsed_seconds=round(elapsed, 2),
    )
    _log({
        "summary": True,
        "files_total": summary.files_total,
        "files_embedded": summary.files_embedded,
        "files_skipped": summary.files_skipped,
        "chunks_embedded": summary.chunks_embedded,
        "embedding_calls": summary.embedding_calls,
        "cost_usd": summary.estimated_cost_usd,
        "elapsed_seconds": summary.elapsed_seconds,
    })
    return summary


def query_similar(
    text: str | None = None,
    k: int = 10,
    *,
    embedding: list[float] | None = None,
    chroma_client: chromadb.api.ClientAPI | None = None,
    embedder: Embedder | None = None,
) -> list[ScoredChunk]:
    client = chroma_client or _default_chroma_client()
    collection = _get_or_create_collection(client)

    if embedding is None:
        if text is None:
            raise ValueError("query_similar requires either text or embedding")
        if embedder is None:
            embedder, _ = _make_openai_embedder(api_key=config.OPENAI_API_KEY)
        embeddings_, _tokens = embedder([text])
        embedding = embeddings_[0]

    res = collection.query(
        query_embeddings=[embedding],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    out: list[ScoredChunk] = []
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append(ScoredChunk(
            file_path=meta.get("file_path", ""),
            subfolder=meta.get("subfolder", ""),
            chunk_index=int(meta.get("chunk_index", 0)),
            text=doc,
            distance=float(dist),
        ))
    return out


# --- CLI ----------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed content/ into Chroma.")
    parser.add_argument("--force", action="store_true",
                        help="Drop and rebuild the collection.")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    config.check_env()
    s = reindex_content_dir(force=args.force, batch_size=args.batch_size)
    print(
        f"Done. files={s.files_total} embedded={s.files_embedded} "
        f"skipped={s.files_skipped} chunks={s.chunks_embedded} "
        f"calls={s.embedding_calls} cost=${s.estimated_cost_usd:.4f} "
        f"elapsed={s.elapsed_seconds:.1f}s"
    )
