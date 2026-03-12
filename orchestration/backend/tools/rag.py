#  Orchestration Engine - RAG Tool
#
#  Direct SQLite queries to noz-rag and verse-rag databases.
#  Reuses the embedding + cosine similarity pattern from noz-rag/server.py.
#
#  Depends on: backend/config.py, tools/base.py
#  Used by:    services/executor.py (via tool registry)

import asyncio
import logging
import sqlite3
import struct
import threading
import time as _time
from typing import Literal

import httpx
import numpy as np

from backend.config import (
    OLLAMA_EMBED_MODEL,
    OLLAMA_EMBED_TIMEOUT,
    OLLAMA_HOSTS,
    RAG_DATABASES,
    RAG_EMBED_DIMENSIONS,
)
from backend.tools.base import Tool

logger = logging.getLogger("orchestration.tools.rag")

# Cooldown before retrying a failed index load (seconds)
_LOAD_RETRY_COOLDOWN = 60


# ---------------------------------------------------------------------------
# RAG database index (loaded once per DB on first use)
# ---------------------------------------------------------------------------

class _RAGIndex:
    """In-memory embedding index for a single RAG database.

    Thread safety: a threading.Lock serializes sync SQLite access so that
    asyncio.to_thread() calls from different threadpool threads don't
    corrupt the connection.
    """

    def __init__(self, db_path: str, dimensions: int):
        self.db_path = db_path
        self.dimensions = dimensions
        self.conn: sqlite3.Connection | None = None
        self.embeddings: np.ndarray | None = None
        self.chunk_ids: list[str] = []
        self.chunk_sources: list[str] = []
        self._state: Literal["unloaded", "loaded", "failed"] = "unloaded"
        self._failed_at: float = 0.0
        self._error: str = ""
        self._lock = threading.Lock()

    def load(self):
        """Load the index. Retries after cooldown on failure.

        Thread safety: guarded by self._lock so concurrent to_thread() calls
        don't race on connection creation or state transitions.
        """
        with self._lock:
            if self._state == "loaded":
                return
            if self._state == "failed" and _time.time() - self._failed_at < _LOAD_RETRY_COOLDOWN:
                return  # Still in cooldown

            try:
                # Close any leftover connection from a previous failed attempt
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None

                self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self.conn.row_factory = sqlite3.Row

                rows = self.conn.execute(
                    "SELECT id, source, embedding FROM chunks WHERE embedding IS NOT NULL"
                ).fetchall()

                if not rows:
                    self._state = "loaded"
                    return

                self.chunk_ids = []
                self.chunk_sources = []
                vectors = []
                for row in rows:
                    self.chunk_ids.append(row["id"])
                    self.chunk_sources.append(row["source"] or "")
                    floats = struct.unpack(f"{self.dimensions}f", row["embedding"])
                    vectors.append(floats)

                self.embeddings = np.array(vectors, dtype=np.float32)
                norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
                norms[norms == 0] = 1
                self.embeddings = self.embeddings / norms

                self._state = "loaded"
            except Exception as e:
                logger.error("Failed to load RAG index %s: %s", self.db_path, e)
                self._state = "failed"
                self._failed_at = _time.time()
                self._error = str(e)

    def query_sync(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Thread-safe sync query on the RAG SQLite connection."""
        with self._lock:
            if self.conn is None:
                return []
            return self.conn.execute(sql, params).fetchall()


class RAGIndexCache:
    """Shared cache of RAG indexes, injected into both RAG tool classes.

    Replaces the old module-level _indexes global with an injectable,
    lifecycle-managed cache.
    """

    def __init__(self):
        self._indexes: dict[str, _RAGIndex] = {}
        self._lock = asyncio.Lock()

    async def get(self, db_name: str) -> _RAGIndex | None:
        """Get or create a RAG index by database name."""
        async with self._lock:
            if db_name not in self._indexes:
                path = RAG_DATABASES.get(db_name)
                if not path:
                    return None
                self._indexes[db_name] = _RAGIndex(path, RAG_EMBED_DIMENSIONS)
            idx = self._indexes[db_name]
        # Load outside the asyncio lock (CPU-bound / sync I/O → thread pool)
        await asyncio.to_thread(idx.load)
        return idx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _embed_query(text: str, http_client: httpx.AsyncClient | None = None) -> np.ndarray | None:
    """Embed a query string via Ollama."""
    host = OLLAMA_HOSTS.get("local", "http://localhost:11434")
    url = f"{host}/api/embeddings"
    body = {"model": OLLAMA_EMBED_MODEL, "prompt": f"search_query: {text}"}

    try:
        if http_client:
            resp = await http_client.post(url, json=body, timeout=OLLAMA_EMBED_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=OLLAMA_EMBED_TIMEOUT) as client:
                resp = await client.post(url, json=body)
        resp.raise_for_status()
        embedding = resp.json().get("embedding", [])
        if len(embedding) != RAG_EMBED_DIMENSIONS:
            return None
        vec = np.array(embedding, dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) or 1)
        return vec
    except Exception as e:
        logger.warning("Embedding request failed: %s", e)
        return None


async def _format_results_async(idx: _RAGIndex, chunk_ids: list[str], scores: dict[str, float]) -> str:
    """Format results using thread-safe queries."""
    parts = []
    for cid in chunk_ids:
        rows = await asyncio.to_thread(idx.query_sync, "SELECT * FROM chunks WHERE id = ?", (cid,))
        if not rows:
            continue
        row = rows[0]
        header_parts = []
        if row["source"]:
            header_parts.append(f"Source: {row['source']}")
        if row["type_name"]:
            header_parts.append(f"Type: {row['type_name']}")
        if row["file_path"]:
            header_parts.append(f"File: {row['file_path']}")
        score_str = f" (score: {scores.get(cid, 0):.3f})"
        header = " | ".join(header_parts)
        parts.append(f"--- [{header}]{score_str} ---\n{row['text']}")
    return "\n\n".join(parts) if parts else "No results found."


def _sanitize_fts_query(raw: str) -> str:
    """Sanitize user input for FTS5 MATCH queries.

    Strips all FTS5 operators and wraps the term in double quotes for
    phrase matching. Returns empty string if nothing usable remains.
    """
    import re
    # Remove FTS5 special characters
    cleaned = re.sub(r'[*()+"^:]', ' ', raw)
    # Remove FTS5 keywords (AND, OR, NOT, NEAR)
    cleaned = re.sub(r'\b(AND|OR|NOT|NEAR)\b', ' ', cleaned, flags=re.IGNORECASE)
    # Remove dashes used as operators (but keep them inside words)
    cleaned = re.sub(r'(?<!\w)-|-(?!\w)', ' ', cleaned)
    # Collapse whitespace
    cleaned = ' '.join(cleaned.split())
    if not cleaned:
        return ""
    # Escape internal quotes and wrap in double quotes for phrase match
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Search Tool
# ---------------------------------------------------------------------------

class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Semantic search across code and documentation RAG databases. "
        "Use this to find code patterns, API signatures, and documentation. "
        "Specify which database to search: 'noz' for NoZ game engine/C# code, "
        "'verse' for Verse/UEFN documentation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language search query"},
            "database": {
                "type": "string",
                "enum": list(RAG_DATABASES.keys()),
                "description": "Which RAG database to search",
            },
            "top_k": {"type": "integer", "default": 5, "description": "Number of results (max 20)"},
            "source_filter": {"type": "string", "default": "", "description": "Filter by source tag"},
        },
        "required": ["query", "database"],
    }

    def __init__(self, cache: RAGIndexCache | None = None, http_client: httpx.AsyncClient | None = None):
        self._cache = cache or RAGIndexCache()
        self._http = http_client

    async def execute(self, params: dict) -> str:
        query = params["query"]
        db_name = params["database"]
        top_k = min(max(1, params.get("top_k", 5)), 20)
        source_filter = params.get("source_filter", "")

        idx = await self._cache.get(db_name)
        if not idx or idx.embeddings is None or idx.conn is None:
            return f"Error: RAG database '{db_name}' not available."

        query_vec = await _embed_query(query, self._http)
        if query_vec is None:
            return "Error: Could not generate embedding. Is Ollama running?"

        similarities = idx.embeddings @ query_vec

        if source_filter:
            for i, src in enumerate(idx.chunk_sources):
                if src != source_filter:
                    similarities[i] = -1

        top_indices = np.argsort(similarities)[::-1][:top_k]
        result_ids = []
        scores = {}
        for i in top_indices:
            if similarities[i] < 0:
                continue
            cid = idx.chunk_ids[i]
            result_ids.append(cid)
            scores[cid] = float(similarities[i])

        return await _format_results_async(idx, result_ids, scores)


# ---------------------------------------------------------------------------
# Lookup Tool
# ---------------------------------------------------------------------------

class LookupTypeTool(Tool):
    name = "lookup_type"
    description = (
        "Look up a specific type, class, or API by exact name in a RAG database. "
        "Uses keyword/FTS search for precise matching."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The type/class/function name to look up"},
            "database": {
                "type": "string",
                "enum": list(RAG_DATABASES.keys()),
                "description": "Which RAG database to search",
            },
            "top_k": {"type": "integer", "default": 5, "description": "Number of results"},
        },
        "required": ["name", "database"],
    }

    def __init__(self, cache: RAGIndexCache | None = None):
        self._cache = cache or RAGIndexCache()

    async def execute(self, params: dict) -> str:
        name = params["name"]
        db_name = params["database"]
        top_k = min(max(1, params.get("top_k", 5)), 20)

        idx = await self._cache.get(db_name)
        if not idx or idx.conn is None:
            return f"Error: RAG database '{db_name}' not available."

        # Exact type_name match
        rows = await asyncio.to_thread(
            idx.query_sync, "SELECT * FROM chunks WHERE type_name = ? LIMIT ?", (name, top_k)
        )
        if rows:
            return await _format_results_async(idx, [r["id"] for r in rows], {})

        # Partial match — use INSTR to avoid wildcard interpretation
        rows = await asyncio.to_thread(
            idx.query_sync,
            "SELECT * FROM chunks WHERE INSTR(LOWER(type_name), LOWER(?)) > 0 LIMIT ?",
            (name, top_k),
        )
        if rows:
            return await _format_results_async(idx, [r["id"] for r in rows], {})

        # FTS fallback — sanitize all FTS5 operators before MATCH
        try:
            safe = _sanitize_fts_query(name)
            if safe:
                rows = await asyncio.to_thread(
                    idx.query_sync,
                    'SELECT chunks.* FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid '
                    'WHERE chunks_fts MATCH ? LIMIT ?',
                    (safe, top_k),
                )
            else:
                rows = []
        except Exception:
            rows = []

        if not rows:
            # Final fallback: plain INSTR on text
            rows = await asyncio.to_thread(
                idx.query_sync,
                "SELECT * FROM chunks WHERE INSTR(LOWER(text), LOWER(?)) > 0 LIMIT ?",
                (name, top_k),
            )

        return await _format_results_async(idx, [r["id"] for r in rows], {}) if rows else f"No results for '{name}'."
