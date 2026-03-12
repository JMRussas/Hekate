#  Orchestration Engine - RAG Tool Tests
#
#  Tests for _RAGIndex, RAGIndexCache, _embed_query,
#  SearchKnowledgeTool.execute, and LookupTypeTool.execute.
#
#  Depends on: backend/tools/rag.py
#  Used by:    pytest

import sqlite3
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from backend.tools.rag import (
    SearchKnowledgeTool,
    LookupTypeTool,
    _RAGIndex,
    _embed_query,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rag_db(tmp_path, name="test.db", chunks=None):
    """Create a RAG SQLite DB with the chunks schema and optional data."""
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY, source TEXT, type_name TEXT,
            file_path TEXT, text TEXT, embedding BLOB
        )
    """)
    conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content=chunks, content_rowid=rowid)"
    )

    if chunks:
        for chunk in chunks:
            conn.execute(
                "INSERT INTO chunks (id, source, type_name, file_path, text, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                chunk,
            )
            # Populate FTS
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text) "
                "SELECT rowid, text FROM chunks WHERE id = ?",
                (chunk[0],),
            )
    conn.commit()
    conn.close()
    return path


def _make_embedding(dimensions=768, seed=42):
    """Create a normalized float32 embedding blob."""
    rng = np.random.RandomState(seed)
    vec = rng.randn(dimensions).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    blob = struct.pack(f"{dimensions}f", *vec)
    return vec, blob


# ---------------------------------------------------------------------------
# TestRAGIndex
# ---------------------------------------------------------------------------

class TestRAGIndex:

    def test_load_transitions_to_loaded(self, tmp_path):
        vec, blob = _make_embedding()
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyClass", "src/MyClass.cs", "class MyClass {}", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()

        assert idx._state == "loaded"
        assert idx.conn is not None
        assert idx.embeddings is not None
        assert len(idx.chunk_ids) == 1

    def test_load_bad_path_sets_failed(self, tmp_path):
        # sqlite3.connect creates a file even if it doesn't exist, but
        # the query for chunks table will fail → state="failed"
        idx = _RAGIndex(str(tmp_path / "nonexistent.db"), 768)
        idx.load()

        assert idx._state == "failed"
        assert idx._failed_at > 0

    def test_cooldown_skips_retry(self, tmp_path):
        idx = _RAGIndex(str(tmp_path / "nonexistent.db"), 768)
        idx.load()
        assert idx._state == "failed"

        # Still in cooldown — should not retry
        idx.load()
        assert idx._state == "failed"

    def test_retry_after_cooldown(self, tmp_path):
        idx = _RAGIndex(str(tmp_path / "nonexistent.db"), 768)
        idx.load()
        assert idx._state == "failed"

        # Fake cooldown expired
        idx._failed_at = time.time() - 61

        # Now create the DB at the path so retry succeeds
        _make_rag_db(tmp_path, name="nonexistent.db")
        idx.load()
        assert idx._state == "loaded"

    def test_empty_db_loads_ok(self, tmp_path):
        path = _make_rag_db(tmp_path, chunks=[])
        idx = _RAGIndex(path, 768)
        idx.load()

        assert idx._state == "loaded"
        assert idx.embeddings is None  # No rows → no embeddings

    def test_already_loaded_is_noop(self, tmp_path):
        vec, blob = _make_embedding()
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyClass", "src/MyClass.cs", "class MyClass {}", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()
        conn_before = idx.conn

        idx.load()  # Should be noop
        assert idx.conn is conn_before  # Same connection object

    def test_query_sync_returns_rows(self, tmp_path):
        vec, blob = _make_embedding()
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyClass", "src/MyClass.cs", "class MyClass {}", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()

        rows = idx.query_sync("SELECT * FROM chunks", ())
        assert len(rows) == 1
        assert rows[0]["id"] == "c1"

    def test_query_sync_no_conn_returns_empty(self):
        idx = _RAGIndex("/fake", 768)
        # Don't load — conn is None
        assert idx.query_sync("SELECT 1", ()) == []


# ---------------------------------------------------------------------------
# TestEmbedQuery
# ---------------------------------------------------------------------------

class TestEmbedQuery:

    @patch("backend.tools.rag.RAG_EMBED_DIMENSIONS", 768)
    async def test_embed_success(self):
        embedding = np.random.randn(768).tolist()

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": embedding}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = await _embed_query("test query", http_client=mock_http)
        assert result is not None
        assert result.shape == (768,)
        assert abs(np.linalg.norm(result) - 1.0) < 0.01

    @patch("backend.tools.rag.RAG_EMBED_DIMENSIONS", 768)
    async def test_wrong_dimensions_returns_none(self):
        # Return 512 floats when 768 expected
        embedding = np.random.randn(512).tolist()

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": embedding}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = await _embed_query("test query", http_client=mock_http)
        assert result is None

    async def test_http_error_returns_none(self):
        import httpx
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await _embed_query("test query", http_client=mock_http)
        assert result is None

    @patch("backend.tools.rag.RAG_EMBED_DIMENSIONS", 768)
    @patch("backend.tools.rag.OLLAMA_EMBED_TIMEOUT", 10)
    @patch("backend.tools.rag.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    async def test_creates_ephemeral_client(self):
        embedding = np.random.randn(768).tolist()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": embedding}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()

        with patch("backend.tools.rag.httpx.AsyncClient", return_value=mock_client):
            result = await _embed_query("test query", http_client=None)

        assert result is not None
        mock_client.post.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestSearchKnowledgeTool
# ---------------------------------------------------------------------------

class TestSearchKnowledgeTool:

    async def test_unavailable_db_returns_error(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        tool = SearchKnowledgeTool(cache=cache)

        result = await tool.execute({"query": "test", "database": "noz"})
        assert "not available" in result

    async def test_no_embeddings_returns_error(self):
        idx = MagicMock()
        idx.embeddings = None
        idx.conn = MagicMock()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = SearchKnowledgeTool(cache=cache)

        result = await tool.execute({"query": "test", "database": "noz"})
        assert "not available" in result

    @patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=None)
    async def test_embed_failure_returns_error(self, _mock_embed):
        idx = MagicMock()
        idx.embeddings = np.ones((1, 768), dtype=np.float32)
        idx.conn = MagicMock()
        idx.chunk_ids = ["c1"]
        idx.chunk_sources = ["engine"]

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = SearchKnowledgeTool(cache=cache)

        result = await tool.execute({"query": "test", "database": "noz"})
        assert "Could not generate embedding" in result

    async def test_successful_search(self, tmp_path):
        vec, blob = _make_embedding(seed=1)
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyClass", "src/MyClass.cs", "class MyClass { int x; }", blob),
        ])

        idx = _RAGIndex(path, 768)
        idx.load()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)

        # Mock the embed to return same vector → high similarity
        with patch("backend.tools.rag._embed_query", new_callable=AsyncMock, return_value=vec):
            tool = SearchKnowledgeTool(cache=cache)
            result = await tool.execute({"query": "MyClass", "database": "noz"})

        assert "MyClass" in result
        assert "Source: engine" in result


# ---------------------------------------------------------------------------
# TestLookupTypeTool
# ---------------------------------------------------------------------------

class TestLookupTypeTool:

    async def test_unavailable_db_returns_error(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        tool = LookupTypeTool(cache=cache)

        result = await tool.execute({"name": "Foo", "database": "noz"})
        assert "not available" in result

    async def test_exact_type_name_match(self, tmp_path):
        vec, blob = _make_embedding()
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyClass", "src/MyClass.cs", "class MyClass {}", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = LookupTypeTool(cache=cache)

        result = await tool.execute({"name": "MyClass", "database": "noz"})
        assert "MyClass" in result

    async def test_partial_match_via_instr(self, tmp_path):
        vec, blob = _make_embedding()
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "MyComplexClass", "src/MyComplexClass.cs", "class MyComplexClass {}", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = LookupTypeTool(cache=cache)

        result = await tool.execute({"name": "Complex", "database": "noz"})
        assert "MyComplexClass" in result

    async def test_fts_fallback(self, tmp_path):
        vec, blob = _make_embedding()
        # type_name doesn't match, but text does via FTS
        path = _make_rag_db(tmp_path, chunks=[
            ("c1", "engine", "OtherClass", "src/Other.cs", "GuardSpawner implementation details", blob),
        ])
        idx = _RAGIndex(path, 768)
        idx.load()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = LookupTypeTool(cache=cache)

        result = await tool.execute({"name": "GuardSpawner", "database": "noz"})
        assert "GuardSpawner" in result

    async def test_no_results(self, tmp_path):
        path = _make_rag_db(tmp_path, chunks=[])
        idx = _RAGIndex(path, 768)
        idx.load()

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=idx)
        tool = LookupTypeTool(cache=cache)

        result = await tool.execute({"name": "NonExistent", "database": "noz"})
        assert "No results for 'NonExistent'" in result
