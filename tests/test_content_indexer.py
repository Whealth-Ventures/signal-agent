"""Smoke tests for src/content_indexer.py — uses Chroma EphemeralClient + a stub embedder."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chromadb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import content_indexer as ci  # noqa: E402


# --- Stub embedder ------------------------------------------------------

def _stub_embedder_factory(dim: int = 8) -> tuple[ci.Embedder, list[int], list[int]]:
    """Returns (embedder, call_count, total_input_count).

    Deterministic embedding: hash each text into a small fixed-dim vector. Lets
    `query_similar` find the same exact text reliably without OpenAI.
    """
    call_count = [0]
    input_count = [0]

    def embed(texts: list[str]) -> tuple[list[list[float]], int]:
        call_count[0] += 1
        input_count[0] += len(texts)
        out: list[list[float]] = []
        tokens = 0
        for t in texts:
            tokens += max(1, len(t) // 4)  # cheap token estimate
            h = hashlib.sha256(t.encode("utf-8")).digest()
            vec = [(h[i % len(h)] / 255.0) for i in range(dim)]
            # L2 normalize so cosine works well
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out, tokens

    return embed, call_count, input_count


def _isolated_client(tmp_root: Path) -> chromadb.api.ClientAPI:
    """A PersistentClient rooted in the test's tmpdir — true isolation between tests.
    EphemeralClient() instances share underlying state, which breaks test isolation.
    """
    return chromadb.PersistentClient(path=str(tmp_root / "chroma"))


# --- Chunking tests -----------------------------------------------------

class ChunkingTest(unittest.TestCase):
    def test_strips_frontmatter(self) -> None:
        text = "---\ntitle: x\ndate: 2026\n---\n\nReal body text."
        self.assertEqual(ci._strip_frontmatter(text).strip(), "Real body text.")

    def test_no_frontmatter_passthrough(self) -> None:
        text = "# Title\n\nBody."
        self.assertEqual(ci._strip_frontmatter(text), text)

    def test_extracts_h1(self) -> None:
        self.assertEqual(ci._extract_h1("# My Title\n\nbody"), "My Title")

    def test_no_h1_returns_empty(self) -> None:
        self.assertEqual(ci._extract_h1("Just text without heading"), "")

    def test_short_file_one_chunk(self) -> None:
        text = "# T\n\nShort body of fewer than 500 words."
        chunks = ci.chunk_markdown(text)
        self.assertEqual(len(chunks), 1)

    def test_long_file_multi_chunks_by_paragraph(self) -> None:
        # 10 paragraphs of 100 words each → ~1000 words → 2 chunks at 500-word target
        para = "word " * 100
        text = "\n\n".join([para.strip()] * 10)
        chunks = ci.chunk_markdown(text, target_words=500)
        self.assertGreater(len(chunks), 1)
        # No paragraph is split
        for c in chunks:
            for line in c.split("\n\n"):
                self.assertTrue(line.strip())

    def test_truncate_to_token_budget(self) -> None:
        # 10K-word paragraph → would exceed token budget → truncated
        big = "word " * 10_000
        chunks = ci.chunk_markdown(big)
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0]), ci.MAX_CHUNK_TOKENS * ci.APPROX_CHARS_PER_TOKEN)


class FileHashTest(unittest.TestCase):
    def test_stable_across_calls(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("hello world")
            path = Path(f.name)
        try:
            self.assertEqual(ci.file_sha256(path), ci.file_sha256(path))
            self.assertEqual(
                ci.file_sha256(path),
                hashlib.sha256(b"hello world").hexdigest(),
            )
        finally:
            path.unlink()


# --- Reindex tests ------------------------------------------------------

class _ReindexBase(unittest.TestCase):
    """Shared scaffolding: tmp content dir, ephemeral chroma, patched logs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.tmp.name) / "content"
        (self.content_dir / "articles_blog").mkdir(parents=True)
        (self.content_dir / "news_press").mkdir(parents=True)

        self._patch_logs = mock.patch.object(
            config, "LOGS_DIR", Path(self.tmp.name) / "logs",
        )
        self._patch_logs.start()
        (Path(self.tmp.name) / "logs").mkdir()

        self.client = _isolated_client(Path(self.tmp.name))
        self.embed, self.call_count, self.input_count = _stub_embedder_factory()

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.tmp.cleanup()

    def _write(self, rel: str, body: str) -> Path:
        p = self.content_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p


class FirstRunTest(_ReindexBase):
    def test_embeds_all_files(self) -> None:
        self._write("articles_blog/a.md", "# A\n\nBody of A.")
        self._write("articles_blog/b.md", "# B\n\nBody of B.")
        self._write("news_press/c.md", "# C\n\nBody of C.")

        stats = ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        self.assertEqual(stats.files_total, 3)
        self.assertEqual(stats.files_embedded, 3)
        self.assertEqual(stats.files_skipped, 0)
        self.assertEqual(stats.chunks_embedded, 3)
        self.assertGreater(self.call_count[0], 0)

    def test_metadata_populated_correctly(self) -> None:
        self._write("articles_blog/a.md", "# Hello\n\nBody.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        coll = self.client.get_collection(ci.COLLECTION_NAME)
        res = coll.get(include=["metadatas"])
        metas = res["metadatas"]
        self.assertEqual(len(metas), 1)
        m = metas[0]
        self.assertEqual(m["file_path"], "articles_blog/a.md")
        self.assertEqual(m["subfolder"], "articles_blog")
        self.assertEqual(m["title"], "Hello")
        self.assertEqual(m["chunk_index"], 0)
        self.assertEqual(m["chunk_total"], 1)
        self.assertEqual(len(m["file_hash"]), 64)


class IdempotenceTest(_ReindexBase):
    def test_second_run_skips_unchanged(self) -> None:
        self._write("articles_blog/a.md", "# A\n\nBody.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        first_calls = self.call_count[0]
        self.assertGreater(first_calls, 0)

        # Reset the call counter (but keep the collection)
        embed2, calls2, _inp2 = _stub_embedder_factory()
        stats = ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=embed2,
            embedding_call_count=calls2,
        )
        self.assertEqual(stats.files_embedded, 0)
        self.assertEqual(stats.files_skipped, 1)
        self.assertEqual(calls2[0], 0)

    def test_modified_file_re_embeds(self) -> None:
        self._write("articles_blog/a.md", "# A\n\nOriginal body.")
        self._write("articles_blog/b.md", "# B\n\nB body.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        # Edit only a.md
        self._write("articles_blog/a.md", "# A\n\nDifferent body now.")

        embed2, calls2, inputs2 = _stub_embedder_factory()
        stats = ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=embed2,
            embedding_call_count=calls2,
        )
        self.assertEqual(stats.files_embedded, 1)  # a.md only
        self.assertEqual(stats.files_skipped, 1)   # b.md
        self.assertEqual(inputs2[0], 1)            # 1 chunk re-embedded

        # Verify a.md's metadata is fresh — only 1 row for that file
        coll = self.client.get_collection(ci.COLLECTION_NAME)
        a_rows = coll.get(where={"file_path": "articles_blog/a.md"})
        self.assertEqual(len(a_rows["ids"]), 1)


class ForceReindexTest(_ReindexBase):
    def test_force_drops_and_rebuilds(self) -> None:
        self._write("articles_blog/a.md", "# A\n\nBody.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )

        embed2, calls2, _inp2 = _stub_embedder_factory()
        stats = ci.reindex_content_dir(
            force=True,
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=embed2,
            embedding_call_count=calls2,
        )
        self.assertEqual(stats.files_embedded, 1)
        self.assertEqual(stats.files_skipped, 0)
        self.assertGreater(calls2[0], 0)


class QuerySimilarTest(_ReindexBase):
    def test_query_returns_chunks_with_metadata(self) -> None:
        self._write("articles_blog/a.md", "# Healthcare AI\n\nAI in primary care.")
        self._write("articles_blog/b.md", "# Pharma\n\nDrug pipelines and biotech.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        # Stub embedder is hash-based, not semantic — query with the exact stored
        # text so the top hit is deterministic. Real OpenAI embeddings would
        # handle paraphrases; the live test exercises that path.
        coll = self.client.get_collection(ci.COLLECTION_NAME)
        stored = coll.get(where={"file_path": "articles_blog/a.md"},
                          include=["documents"])["documents"]
        a_text = stored[0]

        results = ci.query_similar(
            a_text,
            k=2,
            chroma_client=self.client,
            embedder=self.embed,
        )
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIn(r.file_path, {"articles_blog/a.md", "articles_blog/b.md"})
            self.assertGreaterEqual(r.distance, 0.0)
            self.assertTrue(r.text)
        results_sorted = sorted(results, key=lambda r: r.distance)
        self.assertEqual(results_sorted[0].file_path, "articles_blog/a.md")


class LoggingTest(_ReindexBase):
    def test_summary_line_written(self) -> None:
        self._write("articles_blog/a.md", "# A\n\nBody.")
        ci.reindex_content_dir(
            content_dir=self.content_dir,
            chroma_client=self.client,
            embedder=self.embed,
            embedding_call_count=self.call_count,
        )
        log_files = list(config.LOGS_DIR.glob("content_indexer_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        lines = [json.loads(l) for l in log_files[0].read_text().strip().split("\n")]
        # 1 per-file record + 1 summary
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].get("summary"))
        self.assertEqual(lines[-1]["files_embedded"], 1)


@unittest.skipUnless(os.getenv("RUN_LIVE") == "1",
                     "set RUN_LIVE=1 to embed against real OpenAI")
class LiveSmokeTest(unittest.TestCase):
    def test_real_embedding_dim(self) -> None:
        config.check_env()
        embedder, _ = ci._make_openai_embedder(api_key=config.OPENAI_API_KEY)
        embs, tokens = embedder(["hello world"])
        self.assertEqual(len(embs), 1)
        self.assertEqual(len(embs[0]), 1536)
        self.assertGreater(tokens, 0)


if __name__ == "__main__":
    unittest.main()
