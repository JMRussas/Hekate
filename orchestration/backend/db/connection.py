#  Orchestration Engine - Database Connection
#
#  Async SQLite manager with WAL mode and transaction support.
#  Production uses Alembic migrations; tests use inline schema for speed.
#
#  Depends on: backend/db/migrate.py (optional, for production migrations)
#  Used by:    container.py (via DI), tests

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger("orchestration.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_login_at REAL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    requirements TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    config_json TEXT DEFAULT '{}',
    owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    repo_path TEXT,
    git_base_branch TEXT,
    git_project_branch TEXT,
    git_worktree_path TEXT,
    git_state_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    model_used TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
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
    cost_usd REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    wave INTEGER NOT NULL DEFAULT 0,
    phase TEXT,
    verification_status TEXT,
    verification_notes TEXT,
    requirement_ids_json TEXT DEFAULT '[]',
    error TEXT,
    started_at REAL,
    completed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    git_branch TEXT,
    git_commit_sha TEXT,
    claimed_by TEXT,
    claimed_at REAL
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id),
    task_id TEXT REFERENCES tasks(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_periods (
    period_key TEXT PRIMARY KEY,
    period_type TEXT NOT NULL,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    api_call_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    message TEXT,
    data_json TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    checkpoint_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    attempts_json TEXT DEFAULT '[]',
    question TEXT NOT NULL,
    response TEXT,
    resolved_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    provider_email TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_knowledge (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    category TEXT NOT NULL DEFAULT 'discovery',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_task_title TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_token_families (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    is_revoked INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_used_at REAL
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rtf_token_hash
    ON refresh_token_families(token_hash);
CREATE INDEX IF NOT EXISTS idx_rtf_family_id
    ON refresh_token_families(family_id);
CREATE INDEX IF NOT EXISTS idx_rtf_user_id
    ON refresh_token_families(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_provider_uid
    ON user_identities(provider, provider_user_id);
CREATE INDEX IF NOT EXISTS idx_identities_user ON user_identities(user_id);

CREATE INDEX IF NOT EXISTS idx_knowledge_project ON project_knowledge(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_dedup
    ON project_knowledge(project_id, content_hash);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_wave ON tasks(wave);
CREATE INDEX IF NOT EXISTS idx_deps_depends ON task_deps(depends_on);
CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_log(project_id);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_budget_type ON budget_periods(period_type);
CREATE INDEX IF NOT EXISTS idx_events_project ON task_events(project_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);

-- Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project_wave ON tasks(project_id, wave);
CREATE INDEX IF NOT EXISTS idx_events_project_task ON task_events(project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_deps_task_id ON task_deps(task_id);
CREATE INDEX IF NOT EXISTS idx_usage_project_timestamp ON usage_log(project_id, timestamp);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """Async SQLite database with WAL mode.

    Uses aiosqlite which runs SQLite on a dedicated background thread,
    so no threading.Lock is needed on our side.
    """

    def __init__(self):
        self._conn: aiosqlite.Connection | None = None
        self._path: Path | None = None
        self._in_transaction: bool = False
        self._tx_lock: asyncio.Lock = asyncio.Lock()
        self._tx_owner: asyncio.Task | None = None

    async def init(self, db_path: str | Path, *, run_migrations: bool = False):
        """Open or create the database and apply schema.

        Args:
            db_path: Path to the SQLite database file.
            run_migrations: If True, use Alembic migrations (production).
                            If False, use inline schema (tests, faster).
        """
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if run_migrations:
            from backend.db.migrate import run_migrations as _migrate
            await asyncio.to_thread(_migrate, self._path)

        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        if not run_migrations:
            await self._conn.executescript(_SCHEMA)
            await self._conn.commit()

        logger.info("Database initialized at %s", self._path)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call await db.init() first.")
        return self._conn

    @asynccontextmanager
    async def transaction(self):
        """Atomic read+write transaction. Rolls back on exception.

        Uses BEGIN IMMEDIATE to acquire a write lock upfront, preventing
        other writers from interleaving. An asyncio.Lock serializes
        concurrent coroutines sharing the same connection, so a second
        coroutine waits until the first transaction commits/rolls back.

        Safe to nest within the same task — if the current asyncio task
        already owns a transaction, inner calls are no-ops (SQLite doesn't
        support true nested transactions without SAVEPOINTs). Different
        tasks wait on the lock.
        """
        current = asyncio.current_task()
        if self._in_transaction and self._tx_owner is current:
            yield self.conn
            return

        async with self._tx_lock:
            self._in_transaction = True
            self._tx_owner = current
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self.conn
                    await self.conn.commit()
                except Exception:
                    await self.conn.rollback()
                    raise
            finally:
                self._in_transaction = False
                self._tx_owner = None

    async def execute_write(self, sql: str, params: tuple | list = ()) -> aiosqlite.Cursor:
        """Execute a write query and commit.

        Inside a transaction() block, participates in the outer transaction
        (no auto-commit). Outside, auto-commits as before.
        """
        cursor = await self.conn.execute(sql, params)
        if not self._in_transaction:
            await self.conn.commit()
        return cursor

    async def execute_many_write(self, statements: list[tuple[str, tuple | list]]):
        """Execute multiple write statements atomically.

        Uses transaction() internally so all statements commit or roll back
        together.
        """
        async with self.transaction():
            for sql, params in statements:
                await self.conn.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        cursor = await self.conn.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        cursor = await self.conn.execute(sql, params)
        return await cursor.fetchall()

    async def close(self):
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
