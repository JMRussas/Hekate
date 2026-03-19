"""Shared fixtures for Odin god tests."""

import asyncio
import json
import time

import aiosqlite
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# FakeDB — in-memory stub that records calls (no real DB)
# ---------------------------------------------------------------------------

class FakeDB:
    """In-memory DB stub matching DatabaseLike protocol. Records writes."""

    def __init__(self):
        self.writes: list[tuple[str, tuple]] = []
        self._fetchone_result = None
        self._fetchall_result: list = []

    async def execute_write(self, sql: str, params: tuple | list = ()) -> str:
        self.writes.append((sql, tuple(params)))
        return "OK"

    async def fetchone(self, sql: str, params: tuple | list = ()):
        return self._fetchone_result

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list:
        return self._fetchall_result


# ---------------------------------------------------------------------------
# SqliteDB — real async SQLite for integration-style tests
# ---------------------------------------------------------------------------

class SqliteDB:
    """Thin DatabaseLike wrapper around aiosqlite. Translates $N → ?."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @staticmethod
    def _pg_to_sqlite(sql: str) -> str:
        """Translate $1, $2, ... to ? placeholders."""
        import re
        return re.sub(r"\$\d+", "?", sql)

    async def execute_write(self, sql: str, params: tuple | list = ()) -> str:
        sql = self._pg_to_sqlite(sql)
        await self._conn.execute(sql, tuple(params))
        await self._conn.commit()
        return "OK"

    async def fetchone(self, sql: str, params: tuple | list = ()):
        sql = self._pg_to_sqlite(sql)
        async with self._conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list:
        sql = self._pg_to_sqlite(sql)
        async with self._conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_db():
    """In-memory FakeDB for unit tests that don't need real SQL."""
    return FakeDB()


@pytest_asyncio.fixture
async def sqlite_db():
    """Real in-memory SQLite with god tables for integration tests."""
    conn = await aiosqlite.connect(":memory:")

    # Create all god-related tables
    await conn.executescript("""
        CREATE TABLE god_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            god_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT,
            severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_relay_type_created
            ON god_relay_events (event_type, created_at);

        CREATE TABLE god_registry (
            name TEXT PRIMARY KEY,
            port INTEGER,
            status TEXT DEFAULT 'unknown',
            last_heartbeat REAL,
            config TEXT,
            updated_at REAL
        );
    """)
    await conn.commit()

    db = SqliteDB(conn)
    yield db

    await conn.close()
