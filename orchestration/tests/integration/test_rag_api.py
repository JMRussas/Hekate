#  Orchestration Engine - RAG API Tests
#
#  Tests for the read-only RAG database inspection endpoints.
#
#  Depends on: backend/routes/rag.py, tests/conftest.py
#  Used by:    pytest

import sqlite3
from unittest.mock import patch


class TestListDatabases:
    async def test_returns_configured_databases(self, authed_client, tmp_path):
        # Create a mock RAG database
        db_path = str(tmp_path / "test_rag.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chunks ("
            "  id TEXT PRIMARY KEY, source TEXT, type_name TEXT,"
            "  file_path TEXT, text TEXT, embedding BLOB"
            ")"
        )
        conn.execute(
            "INSERT INTO chunks (id, source, type_name, text) "
            "VALUES ('c1', 'engine', 'Graphics', 'Draw a sprite')"
        )
        conn.execute(
            "INSERT INTO chunks (id, source, type_name, text) "
            "VALUES ('c2', 'engine', 'Audio', 'Play a sound')"
        )
        conn.execute(
            "INSERT INTO chunks (id, source, type_name, text) "
            "VALUES ('c3', 'docs', 'README', 'Getting started')"
        )
        conn.commit()
        conn.close()

        with patch("backend.routes.rag.RAG_DATABASES", {"test": db_path}):
            resp = await authed_client.get("/api/rag/databases")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test"
        assert data[0]["exists"] is True
        assert data[0]["chunk_count"] == 3
        assert data[0]["source_count"] == 2

    async def test_missing_database_shows_not_exists(self, authed_client):
        with patch("backend.routes.rag.RAG_DATABASES", {"missing": "/nonexistent/path.db"}):
            resp = await authed_client.get("/api/rag/databases")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["exists"] is False
        assert data[0]["chunk_count"] == 0

    async def test_empty_config_returns_empty(self, authed_client):
        with patch("backend.routes.rag.RAG_DATABASES", {}):
            resp = await authed_client.get("/api/rag/databases")
        assert resp.json() == []

    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.get("/api/rag/databases")
        assert resp.status_code in (401, 403)


class TestListSources:
    async def test_returns_sources_with_counts(self, authed_client, tmp_path):
        db_path = str(tmp_path / "sources_rag.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chunks (id TEXT PRIMARY KEY, source TEXT, type_name TEXT, text TEXT, embedding BLOB)"
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO chunks (id, source, text) VALUES (?, 'engine', 'text')",
                (f"e{i}",),
            )
        for i in range(3):
            conn.execute(
                "INSERT INTO chunks (id, source, text) VALUES (?, 'docs', 'text')",
                (f"d{i}",),
            )
        conn.commit()
        conn.close()

        with patch("backend.routes.rag.RAG_DATABASES", {"test": db_path}):
            resp = await authed_client.get("/api/rag/databases/test/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Sorted by count DESC
        assert data[0]["source"] == "engine"
        assert data[0]["count"] == 5
        assert data[1]["source"] == "docs"
        assert data[1]["count"] == 3

    async def test_unknown_database_returns_empty(self, authed_client):
        with patch("backend.routes.rag.RAG_DATABASES", {}):
            resp = await authed_client.get("/api/rag/databases/nonexistent/sources")
        assert resp.json() == []


class TestListDocuments:
    async def test_returns_paginated_chunks(self, authed_client, tmp_path):
        db_path = str(tmp_path / "docs_rag.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chunks ("
            "  id TEXT PRIMARY KEY, source TEXT, type_name TEXT,"
            "  file_path TEXT, text TEXT, embedding BLOB"
            ")"
        )
        for i in range(10):
            conn.execute(
                "INSERT INTO chunks (id, source, type_name, text) "
                "VALUES (?, 'engine', ?, ?)",
                (f"c{i}", f"Type{i}", f"Content for chunk {i}"),
            )
        conn.commit()
        conn.close()

        with patch("backend.routes.rag.RAG_DATABASES", {"test": db_path}):
            resp = await authed_client.get(
                "/api/rag/databases/test/documents?limit=3&offset=0"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 10
        assert len(data["items"]) == 3
        assert data["items"][0]["source"] == "engine"
        assert "Content for chunk" in data["items"][0]["text_preview"]

    async def test_filter_by_source(self, authed_client, tmp_path):
        db_path = str(tmp_path / "filter_rag.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chunks ("
            "  id TEXT PRIMARY KEY, source TEXT, type_name TEXT,"
            "  file_path TEXT, text TEXT, embedding BLOB"
            ")"
        )
        conn.execute("INSERT INTO chunks (id, source, text) VALUES ('a', 'engine', 'a')")
        conn.execute("INSERT INTO chunks (id, source, text) VALUES ('b', 'docs', 'b')")
        conn.commit()
        conn.close()

        with patch("backend.routes.rag.RAG_DATABASES", {"test": db_path}):
            resp = await authed_client.get(
                "/api/rag/databases/test/documents?source=engine"
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "a"

    async def test_unknown_database_returns_empty(self, authed_client):
        with patch("backend.routes.rag.RAG_DATABASES", {}):
            resp = await authed_client.get("/api/rag/databases/nope/documents")
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_text_preview_truncated(self, authed_client, tmp_path):
        db_path = str(tmp_path / "preview_rag.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chunks ("
            "  id TEXT PRIMARY KEY, source TEXT, type_name TEXT,"
            "  file_path TEXT, text TEXT, embedding BLOB"
            ")"
        )
        long_text = "x" * 500
        conn.execute(
            "INSERT INTO chunks (id, source, text) VALUES ('long', 'docs', ?)",
            (long_text,),
        )
        conn.commit()
        conn.close()

        with patch("backend.routes.rag.RAG_DATABASES", {"test": db_path}):
            resp = await authed_client.get("/api/rag/databases/test/documents")
        data = resp.json()
        assert len(data["items"][0]["text_preview"]) == 200
