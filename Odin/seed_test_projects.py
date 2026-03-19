"""Seed 5 test projects into the orchestration DB.

Run this, then start the pipeline:
    python seed_test_projects.py
    python run_pipeline.py --max-concurrent 2

The pipeline will plan, dispatch, execute, and verify all 5 projects.
"""

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "orchestration"))

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "orchestration", "data", "orchestration.db")


class SqliteDB:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _tr(sql):
        import re
        return re.sub(r'\$\d+', '?', sql)

    async def execute_write(self, sql, params=()):
        await self._conn.execute(self._tr(sql), params)
        await self._conn.commit()


TEST_PROJECTS = [
    {
        "name": "Pipeline Test 1: Health Endpoint",
        "requirements": (
            "Add a /api/health/detailed endpoint to the orchestration FastAPI app. "
            "It should return JSON with: service version (read from a VERSION constant), "
            "uptime in seconds (since app start), database connection status (ok/error), "
            "and current timestamp. No authentication required. "
            "Add a test that verifies the endpoint returns 200 with all required fields."
        ),
    },
    {
        "name": "Pipeline Test 2: Request Logging",
        "requirements": (
            "Add request logging middleware to the orchestration FastAPI app. "
            "Create an Alembic migration for a request_log table with columns: "
            "id, method, path, status_code, duration_ms, timestamp. "
            "Create middleware that logs every request to this table. "
            "Create a GET /api/admin/request-logs endpoint (admin-only) that returns "
            "the last 100 log entries. Write tests for the middleware and endpoint."
        ),
    },
    {
        "name": "Pipeline Test 3: DB Adapter Research",
        "requirements": (
            "Research task: evaluate whether the orchestration engine should add "
            "a Postgres backend alongside SQLite. Consider: what queries would benefit "
            "from Postgres, what's the migration path, what breaks. "
            "Then write a DatabaseBackend protocol class that both SQLite and Postgres "
            "adapters would implement, with async methods for execute_write, fetchone, "
            "fetchall, and transaction context manager."
        ),
    },
    {
        "name": "Pipeline Test 4: Budget Context Manager",
        "requirements": (
            "Refactor the budget service (orchestration/backend/services/budget.py) "
            "to use an async context manager for spend recording. The context manager "
            "should: check budget before execution, record actual spend after, and "
            "rollback the status update if the operation fails. Write tests that verify "
            "budget is checked before and recorded after execution."
        ),
    },
    {
        "name": "Pipeline Test 5: Config Validator with TDD",
        "requirements": (
            "Create a config_validator.py module in orchestration/backend/ that validates "
            "config.json against a schema. Requirements: "
            "1. Validate all required fields exist (server.host, server.port, auth.secret_key) "
            "2. Validate types (port is int, cors_origins is list) "
            "3. Validate auth.secret_key is at least 32 chars and not a placeholder "
            "4. Return structured validation errors (field path, expected, actual) "
            "IMPORTANT: Write the tests FIRST, then implement. Follow TDD red-green-refactor."
        ),
    },
]


async def seed():
    db_path = os.path.abspath(DB_PATH)
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        return

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    db = SqliteDB(conn)

    # Ensure relay table exists
    await db.execute_write("""
        CREATE TABLE IF NOT EXISTS god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL, source TEXT NOT NULL,
            payload TEXT DEFAULT '{}', severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL
        )
    """)

    repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for i, proj in enumerate(TEST_PROJECTS, 1):
        pid = uuid.uuid4().hex[:12]
        now = time.time()

        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, repo_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, proj["name"], proj["requirements"], "draft", repo_path, now, now),
        )

        # Inject project_created event
        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("project_created", "user",
             json.dumps({"project_id": pid}),
             "info", now),
        )

        print(f"  [{i}/5] Created: {proj['name']} (id={pid})")

    # Inject a global tick to kick off dispatch after planning
    await db.execute_write(
        "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tick", "scheduler", "{}", "info", time.time()),
    )

    await conn.close()
    print()
    print("All 5 projects seeded. Run the pipeline:")
    print("  cd Odin")
    print("  python run_pipeline.py --max-concurrent 2")
    print()
    print("The pipeline will:")
    print("  1. Athena plans each project (via Gemini)")
    print("  2. Odin starts them and dispatches tasks")
    print("  3. Hermes executes via Claude Code CLI (async, parallel)")
    print("  4. Mimir verifies output")
    print("  5. Odin checks wave/project completion")


if __name__ == "__main__":
    print("Seeding 5 test projects...")
    print()
    asyncio.run(seed())
