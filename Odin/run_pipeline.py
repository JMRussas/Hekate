"""Standalone pipeline runner.

Usage:
    python run_pipeline.py                      # Start pipeline, wait for events
    python run_pipeline.py --create "Build X"   # Create a test project and run
    python run_pipeline.py --inject proj-id     # Inject project_created for existing project

The pipeline reads from the orchestration SQLite DB and processes events
through the full handler chain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid

# Add Odin to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Add orchestration backend to path (for planner imports in athena)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "orchestration"))

import aiosqlite

from gods.pipeline import Pipeline, Emit
from gods.handlers.registration import register_all_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline.runner")

# Default DB path
DB_PATH = os.environ.get(
    "ORCHESTRATION_DB",
    os.path.join(os.path.dirname(__file__), "..", "orchestration", "data", "orchestration.db"),
)


# ---------------------------------------------------------------------------
# Thin DB adapter — wraps aiosqlite to match the protocol our handlers expect
# ---------------------------------------------------------------------------

class SqliteDB:
    """Adapter that translates $N placeholders to ? for SQLite."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @staticmethod
    def _translate(sql: str) -> str:
        """Convert $1, $2, ... to ? for SQLite."""
        import re
        return re.sub(r'\$\d+', '?', sql)

    async def execute_write(self, sql: str, params: tuple = ()):
        sql = self._translate(sql)
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()):
        sql = self._translate(sql)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()):
        sql = self._translate(sql)
        async with self._conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def execute_many_write(self, statements: list[tuple[str, tuple | list]]):
        """Execute multiple write statements atomically."""
        for sql, params in statements:
            await self._conn.execute(self._translate(sql), tuple(params))
        await self._conn.commit()


# ---------------------------------------------------------------------------
# Ensure relay tables exist
# ---------------------------------------------------------------------------

async def ensure_tables(db: SqliteDB):
    await db.execute_write("""
        CREATE TABLE IF NOT EXISTS god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL
        )
    """)
    await db.execute_write("""
        CREATE INDEX IF NOT EXISTS idx_relay_type_created
            ON god_relay_events (event_type, created_at)
    """)
    await db.execute_write("""
        CREATE TABLE IF NOT EXISTS god_registry (
            name TEXT PRIMARY KEY,
            last_seen_id INTEGER DEFAULT 0,
            last_heartbeat REAL DEFAULT 0,
            config_json TEXT DEFAULT '{}'
        )
    """)
    logger.info("Relay tables ready")


# ---------------------------------------------------------------------------
# Inject events
# ---------------------------------------------------------------------------

async def inject_project_created(db: SqliteDB, project_id: str):
    """Insert a project_created event into the relay table."""
    await db.execute_write(
        "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("project_created", "user",
         json.dumps({"project_id": project_id}),
         "info", time.time()),
    )
    logger.info("Injected project_created for %s", project_id)


async def create_test_project(db: SqliteDB, name: str) -> str:
    """Create a minimal project + task for testing."""
    pid = uuid.uuid4().hex[:12]
    now = time.time()

    await db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, repo_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, name, name, "draft",
         os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
         now, now),
    )

    plan_id = uuid.uuid4().hex[:12]
    tid = uuid.uuid4().hex[:12]
    await db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "status, wave, retry_count, max_retries, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, pid, plan_id, f"Implement: {name}", name, "code", "pending", 0, 0, 3, now, now),
    )

    logger.info("Created project %s with task %s", pid, tid)
    return pid


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args):
    db_path = os.path.abspath(DB_PATH)
    logger.info("Using DB: %s", db_path)

    if not os.path.exists(db_path):
        logger.error("DB not found: %s", db_path)
        return

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = aiosqlite.Row
    db = SqliteDB(conn)

    await ensure_tables(db)

    # Build pipeline with async hermes
    pipeline = Pipeline(db)
    hermes = register_all_handlers(
        pipeline,
        max_concurrent=args.max_concurrent,
    )
    await pipeline.restore_cursor()

    logger.info("Pipeline ready — %d handlers registered, hermes max_concurrent=%d",
                len(pipeline._handlers), args.max_concurrent)

    # Handle --reset
    if args.reset:
        logger.info("Resetting pipeline state...")
        await pipeline.reset()
        logger.info("Pipeline reset complete — cursor at 0, relay cleared")

    # Handle --create or --inject
    if args.create:
        pid = await create_test_project(db, args.create)
        await inject_project_created(db, pid)
    elif args.inject:
        await inject_project_created(db, args.inject)

    # Run pipeline
    if args.once:
        # Single tick — useful for testing
        logger.info("Running single tick...")
        await pipeline.tick()
        logger.info("Tick complete")
    else:
        # Continuous mode with tick scheduler
        pipeline.start_scheduler(interval=args.tick_interval)
        logger.info("Starting pipeline loop (tick every %.1fs, Ctrl+C to stop)...",
                     args.tick_interval)
        try:
            tick_count = 0
            while True:
                await pipeline.tick()
                tick_count += 1
                in_flight = len(hermes.in_flight)
                if tick_count % 30 == 0 or in_flight > 0:
                    logger.info("Pipeline alive — tick %d, cursor %d, in-flight %d/%d",
                                tick_count, pipeline._last_seen_id,
                                in_flight, args.max_concurrent)
                await asyncio.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            pipeline.stop_scheduler()
            await hermes.shutdown(timeout=30.0)
            logger.info("Pipeline stopped")

    await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Standalone pipeline runner")
    parser.add_argument("--create", type=str, help="Create a test project with this name")
    parser.add_argument("--inject", type=str, help="Inject project_created for this project ID")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit")
    parser.add_argument("--db", type=str, help="Path to orchestration.db")
    parser.add_argument("--reset", action="store_true", help="Clear relay table and reset cursor before starting")
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max parallel CLI tasks")
    parser.add_argument("--tick-interval", type=float, default=5.0, help="Tick scheduler interval (seconds)")
    args = parser.parse_args()

    if args.db:
        global DB_PATH
        DB_PATH = args.db

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
