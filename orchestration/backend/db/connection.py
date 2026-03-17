#  Orchestration Engine - Database Connection
#
#  Dual-backend async database: Postgres (production) or SQLite (local dev).
#  All queries use $1, $2 Postgres-style placeholders everywhere.
#  The SQLite backend translates $N → ? automatically.
#
#  Backend selection:
#    - Postgres DSN (postgresql://...) → asyncpg connection pool
#    - File path (*.db) → aiosqlite single connection
#    - Default: ORCHESTRATION_DSN env var, or SQLite at data/orchestration.db
#
#  Depends on: backend/db/migrate.py (optional, for production migrations)
#  Used by:    container.py (via DI), tests

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger("orchestration.db")

# Default: local SQLite (zero-dependency). Set ORCHESTRATION_DSN for Postgres.
_DEFAULT_SQLITE = "data/orchestration.db"


# ---------------------------------------------------------------------------
# SQL translation — $1,$2 → ? for SQLite
# ---------------------------------------------------------------------------

_PG_PARAM_RE = re.compile(r"\$\d+")


def _pg_to_sqlite(sql: str) -> str:
    """Convert Postgres SQL to SQLite-compatible SQL at runtime."""
    s = _PG_PARAM_RE.sub("?", sql)
    # INSERT INTO ... ON CONFLICT DO NOTHING → INSERT OR IGNORE INTO ...
    s = re.sub(
        r"INSERT\s+INTO\s+(.+?)\s+ON\s+CONFLICT\s+DO\s+NOTHING",
        lambda m: f"INSERT OR IGNORE INTO {m.group(1)}",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # ON CONFLICT(...) DO UPDATE → SQLite also supports this, leave it
    return s


def _pg_schema_to_sqlite(sql: str) -> str:
    """Convert Postgres DDL to SQLite-compatible DDL."""
    s = sql.replace("DOUBLE PRECISION", "REAL")
    s = s.replace("GENERATED ALWAYS AS IDENTITY", "AUTOINCREMENT")
    s = s.replace("ON CONFLICT DO NOTHING", "OR IGNORE")
    return s


# ---------------------------------------------------------------------------
# Schema (used by tests for fast inline setup, production uses Alembic)
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT,
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at DOUBLE PRECISION NOT NULL,
        last_login_at DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        requirements TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        completed_at DOUBLE PRECISION,
        config_json TEXT DEFAULT '{}',
        owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,
        repo_path TEXT,
        git_base_branch TEXT,
        git_project_branch TEXT,
        git_worktree_path TEXT,
        git_state_json TEXT DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 1,
        model_used TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        plan_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        node_mapping TEXT,
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        task_type TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 50,
        status TEXT NOT NULL DEFAULT 'pending',
        model_tier TEXT NOT NULL DEFAULT 'haiku',
        model_used TEXT,
        context_json TEXT DEFAULT '[]',
        tools_json TEXT DEFAULT '[]',
        system_prompt TEXT DEFAULT '',
        output_text TEXT,
        output_artifacts_json TEXT DEFAULT '[]',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        max_tokens INTEGER NOT NULL DEFAULT 4096,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 2,
        wave INTEGER NOT NULL DEFAULT 0,
        phase TEXT,
        verification_status TEXT,
        verification_notes TEXT,
        requirement_ids_json TEXT DEFAULT '[]',
        rationale TEXT,
        error TEXT,
        started_at DOUBLE PRECISION,
        completed_at DOUBLE PRECISION,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        git_branch TEXT,
        git_commit_sha TEXT,
        claimed_by TEXT,
        claimed_at DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS task_deps (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, depends_on)
    )""",
    """CREATE TABLE IF NOT EXISTS usage_log (
        id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
        project_id TEXT REFERENCES projects(id),
        task_id TEXT REFERENCES tasks(id),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        cost_usd DOUBLE PRECISION NOT NULL,
        purpose TEXT NOT NULL DEFAULT '',
        timestamp DOUBLE PRECISION NOT NULL,
        context_tokens_injected INTEGER,
        source_node_count INTEGER,
        enrichment_latency_ms DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS budget_periods (
        period_key TEXT PRIMARY KEY,
        period_type TEXT NOT NULL,
        total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
        total_completion_tokens INTEGER NOT NULL DEFAULT 0,
        api_call_count INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
        event_type TEXT NOT NULL,
        message TEXT,
        data_json TEXT,
        timestamp DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
        checkpoint_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        attempts_json TEXT DEFAULT '[]',
        question TEXT NOT NULL,
        response TEXT,
        resolved_at DOUBLE PRECISION,
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_identities (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        provider_user_id TEXT NOT NULL,
        provider_email TEXT,
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS project_knowledge (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
        category TEXT NOT NULL DEFAULT 'discovery',
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        alternatives_considered TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT 'medium',
        source_task_title TEXT,
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS refresh_token_families (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        family_id TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        is_revoked INTEGER DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION
    )""",
    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)",
    "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rtf_token_hash ON refresh_token_families(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_rtf_family_id ON refresh_token_families(family_id)",
    "CREATE INDEX IF NOT EXISTS idx_rtf_user_id ON refresh_token_families(user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_provider_uid ON user_identities(provider, provider_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_identities_user ON user_identities(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_project ON project_knowledge(project_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_dedup ON project_knowledge(project_id, content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_wave ON tasks(wave)",
    "CREATE INDEX IF NOT EXISTS idx_deps_depends ON task_deps(depends_on)",
    "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_log(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_budget_type ON budget_periods(period_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON task_events(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project_wave ON tasks(project_id, wave)",
    "CREATE INDEX IF NOT EXISTS idx_events_project_task ON task_events(project_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_deps_task_id ON task_deps(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_project_timestamp ON usage_log(project_id, timestamp)",
    # Sentinel tables
    """CREATE TABLE IF NOT EXISTS sentinel_observations (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        rule TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        details_json TEXT DEFAULT '{}',
        reasoning TEXT,
        timestamp DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sentinel_decisions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        observation_id TEXT,
        command TEXT NOT NULL,
        params_json TEXT DEFAULT '{}',
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        reasoning TEXT,
        details_json TEXT DEFAULT '{}',
        timestamp DOUBLE PRECISION NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sentinel_obs_project ON sentinel_observations(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_sentinel_dec_project ON sentinel_decisions(project_id)",
]


# ---------------------------------------------------------------------------
# Cursor result wrapper (for aiosqlite-style cursor = conn.execute(); cursor.fetchone())
# ---------------------------------------------------------------------------

class _CursorResult:
    """Mimics aiosqlite cursor for callers that do cursor = await conn.execute(SELECT)."""

    __slots__ = ("_rows",)

    def __init__(self, rows):
        self._rows = list(rows) if not isinstance(rows, list) else rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------

class _PostgresBackend:
    """asyncpg connection pool backend."""

    def __init__(self):
        self._pool = None

    @property
    def is_postgres(self):
        return True

    async def init(self, dsn: str, run_migrations: bool):
        import asyncpg
        if run_migrations:
            from backend.db.migrate import run_migrations as _migrate
            await asyncio.to_thread(_migrate, dsn)

        self._pool = await asyncpg.create_pool(
            dsn, min_size=2, max_size=10, command_timeout=60,
        )

        if not run_migrations:
            async with self._pool.acquire() as conn:
                for stmt in _SCHEMA_STATEMENTS:
                    await conn.execute(stmt)

        logger.info("Postgres backend initialized (pool: 2-10)")

    @asynccontextmanager
    async def transaction(self):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _PgConnProxy(conn)

    async def execute_write(self, sql, params):
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *params)

    async def fetchone(self, sql, params):
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *params)

    async def fetchall(self, sql, params):
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None


class _PgConnProxy:
    """Wraps asyncpg connection: unpacks tuple params, returns CursorResult for SELECTs."""

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def _unpack(self, params):
        return tuple(params) if params else ()

    async def execute(self, sql, params=(), *args):
        unpacked = self._unpack(params) if not args else (params, *args)
        stripped = sql.strip().upper()
        if stripped.startswith("SELECT") or stripped.startswith("WITH"):
            rows = await self._conn.fetch(sql, *unpacked)
            return _CursorResult(rows)
        return await self._conn.execute(sql, *unpacked)

    async def fetchrow(self, sql, params=()):
        return await self._conn.fetchrow(sql, *self._unpack(params))

    async def fetch(self, sql, params=()):
        return await self._conn.fetch(sql, *self._unpack(params))

    async def fetchone(self, sql, params=()):
        return await self.fetchrow(sql, params)

    async def fetchall(self, sql, params=()):
        return await self.fetch(sql, params)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class _SqliteBackend:
    """aiosqlite single-connection backend for local dev."""

    def __init__(self):
        self._conn = None
        self._path = None
        self._in_transaction = False
        self._tx_lock = asyncio.Lock()
        self._tx_owner = None

    @property
    def is_postgres(self):
        return False

    async def init(self, db_path: str, run_migrations: bool):
        import aiosqlite

        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if run_migrations:
            from backend.db.migrate import run_migrations as _migrate
            await asyncio.to_thread(_migrate, self._path)

        self._conn = await aiosqlite.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        if not run_migrations:
            schema = "\n".join(_pg_schema_to_sqlite(s) + ";" for s in _SCHEMA_STATEMENTS)
            await self._conn.executescript(schema)
            await self._conn.commit()

        logger.info("SQLite backend initialized at %s", self._path)

    @asynccontextmanager
    async def transaction(self):
        current = asyncio.current_task()
        if self._in_transaction and self._tx_owner is current:
            yield _SqliteConnProxy(self._conn)
            return

        async with self._tx_lock:
            self._in_transaction = True
            self._tx_owner = current
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield _SqliteConnProxy(self._conn)
                    await self._conn.commit()
                except Exception:
                    await self._conn.rollback()
                    raise
            finally:
                self._in_transaction = False
                self._tx_owner = None

    async def execute_write(self, sql, params):
        sql = _pg_to_sqlite(sql)
        cursor = await self._conn.execute(sql, tuple(params))
        if not self._in_transaction:
            await self._conn.commit()
        return f"OK {cursor.rowcount}"

    async def fetchone(self, sql, params):
        sql = _pg_to_sqlite(sql)
        cursor = await self._conn.execute(sql, tuple(params))
        return await cursor.fetchone()

    async def fetchall(self, sql, params):
        sql = _pg_to_sqlite(sql)
        cursor = await self._conn.execute(sql, tuple(params))
        return await cursor.fetchall()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None


class _SqliteConnProxy:
    """Wraps aiosqlite connection: translates $N → ? placeholders."""

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=(), *args):
        sql = _pg_to_sqlite(sql)
        all_params = (params, *args) if args else tuple(params)
        cursor = await self._conn.execute(sql, all_params)
        stripped = sql.strip().upper()
        if stripped.startswith("SELECT") or stripped.startswith("WITH"):
            rows = await cursor.fetchall()
            return _CursorResult(rows)
        return cursor

    async def fetchrow(self, sql, params=()):
        sql = _pg_to_sqlite(sql)
        cursor = await self._conn.execute(sql, tuple(params))
        return await cursor.fetchone()

    async def fetch(self, sql, params=()):
        sql = _pg_to_sqlite(sql)
        cursor = await self._conn.execute(sql, tuple(params))
        return await cursor.fetchall()

    async def fetchone(self, sql, params=()):
        return await self.fetchrow(sql, params)

    async def fetchall(self, sql, params=()):
        return await self.fetch(sql, params)


# ---------------------------------------------------------------------------
# Database class — unified interface
# ---------------------------------------------------------------------------

def _is_postgres_dsn(value: str) -> bool:
    return value.startswith("postgresql://") or value.startswith("postgres://")


class Database:
    """Dual-backend async database.

    Postgres (asyncpg pool) when DSN is postgresql://.
    SQLite (aiosqlite) when path is a file.
    All queries use $1, $2 placeholders — SQLite backend translates to ?.
    """

    def __init__(self):
        self._backend: _PostgresBackend | _SqliteBackend | None = None

    @property
    def is_postgres(self) -> bool:
        return self._backend is not None and self._backend.is_postgres

    async def init(self, db_path: str | Path | None = None, *, run_migrations: bool = False):
        """Initialize the database connection.

        Args:
            db_path: Postgres DSN or SQLite file path.
                     Falls back to ORCHESTRATION_DSN env var, then SQLite default.
            run_migrations: If True, use Alembic (production). If False, inline schema.
        """
        target = str(db_path) if db_path else os.environ.get("ORCHESTRATION_DSN", "")

        if _is_postgres_dsn(target):
            self._backend = _PostgresBackend()
            await self._backend.init(target, run_migrations)
        else:
            # SQLite — use provided path or default
            path = target if target and not _is_postgres_dsn(target) else _DEFAULT_SQLITE
            self._backend = _SqliteBackend()
            await self._backend.init(path, run_migrations)

    @asynccontextmanager
    async def transaction(self):
        """Atomic transaction. Rolls back on exception."""
        async with self._backend.transaction() as conn:
            yield conn

    async def execute_write(self, sql: str, params: tuple | list = ()) -> str:
        """Execute a write query."""
        return await self._backend.execute_write(sql, params)

    async def execute_many_write(self, statements: list[tuple[str, tuple | list]]):
        """Execute multiple write statements atomically."""
        async with self.transaction() as conn:
            for sql, params in statements:
                await conn.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple | list = ()):
        return await self._backend.fetchone(sql, params)

    async def fetchall(self, sql: str, params: tuple | list = ()):
        return await self._backend.fetchall(sql, params)

    async def close(self):
        """Close the database connection/pool."""
        if self._backend:
            await self._backend.close()
            self._backend = None
