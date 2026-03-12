#  Orchestration Engine - RAG Routes
#
#  Read-only endpoints for inspecting configured RAG databases.
#  Indexing is managed externally (noz-rag / verse-rag pipelines).
#
#  Depends on: config.py, tools/rag.py, container.py
#  Used by:    app.py

import os
import sqlite3

import asyncio
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query

from backend.config import RAG_DATABASES
from backend.container import Container
from backend.models.schemas import RAGChunkPreview, RAGDatabaseInfo
from backend.tools.rag import RAGIndexCache

router = APIRouter(prefix="/rag", tags=["rag"])


@router.get("/databases")
@inject
async def list_databases(
    rag_cache: RAGIndexCache = Depends(Provide[Container.rag_cache]),
) -> list[RAGDatabaseInfo]:
    """List all configured RAG databases with metadata."""
    results = []
    for name, path in RAG_DATABASES.items():
        exists = os.path.isfile(path)
        info = RAGDatabaseInfo(
            name=name,
            path=name,  # Expose name only, not filesystem path
            exists=exists,
        )

        if exists:
            try:
                file_size = os.path.getsize(path)
                info.file_size_bytes = file_size

                # Query chunk/source counts via sync sqlite3 on thread
                def _query_stats(db_path):
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    try:
                        chunk_count = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM chunks"
                        ).fetchone()["cnt"]
                        source_rows = conn.execute(
                            "SELECT source, COUNT(*) AS cnt FROM chunks GROUP BY source ORDER BY cnt DESC"
                        ).fetchall()
                        return chunk_count, [
                            {"source": r["source"] or "(none)", "count": r["cnt"]}
                            for r in source_rows
                        ]
                    finally:
                        conn.close()

                chunk_count, sources = await asyncio.to_thread(_query_stats, path)
                info.chunk_count = chunk_count
                info.source_count = len(sources)
                info.sources = sources

                # Check if the index is loaded in the cache
                idx = rag_cache._indexes.get(name)
                if idx is not None:
                    info.index_status = idx._state
                else:
                    info.index_status = "unloaded"
            except Exception:
                info.index_status = "error"

        results.append(info)
    return results


@router.get("/databases/{name}/sources")
@inject
async def list_sources(
    name: str,
    rag_cache: RAGIndexCache = Depends(Provide[Container.rag_cache]),
) -> list[dict]:
    """List sources in a RAG database with chunk counts."""
    path = RAG_DATABASES.get(name)
    if not path or not os.path.isfile(path):
        return []

    def _query_sources(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS cnt FROM chunks GROUP BY source ORDER BY cnt DESC"
            ).fetchall()
            return [{"source": r["source"] or "(none)", "count": r["cnt"]} for r in rows]
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_query_sources, path)
    except Exception:
        return []


@router.get("/databases/{name}/documents")
@inject
async def list_documents(
    name: str,
    source: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    rag_cache: RAGIndexCache = Depends(Provide[Container.rag_cache]),
) -> dict:
    """List chunks in a RAG database with text preview (paginated)."""
    path = RAG_DATABASES.get(name)
    if not path or not os.path.isfile(path):
        return {"total": 0, "items": []}

    def _query_docs(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            where = ""
            params: list = []
            if source:
                where = "WHERE source = ?"
                params.append(source)

            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM chunks {where}", params
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"SELECT id, source, type_name, file_path, text FROM chunks {where} "
                f"ORDER BY source, id LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

            items = []
            for r in rows:
                text = r["text"] or ""
                items.append(RAGChunkPreview(
                    id=r["id"],
                    source=r["source"] or "",
                    type_name=r["type_name"],
                    file_path=os.path.basename(r["file_path"]) if r["file_path"] else None,
                    text_preview=text[:200],
                ))
            return total, items
        finally:
            conn.close()

    try:
        total, items = await asyncio.to_thread(_query_docs, path)
    except Exception:
        return {"total": 0, "items": []}
    return {"total": total, "items": items}
