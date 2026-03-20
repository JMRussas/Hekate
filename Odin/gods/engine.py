"""Hekate Engine — unified orchestration.

Replaces the monolith. One process that:
  1. Runs the gods pipeline as a background task
  2. Creates projects and emits events
  3. Streams relay events via SSE
  4. Plans via LLM gateway (no monolith imports)
  5. Decomposes plans into task rows
  6. Queries project/task status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiosqlite

from gods.pipeline import Pipeline, Event, Emit
from gods.handlers.registration import register_all_handlers

logger = logging.getLogger("gods.engine")

# ---------------------------------------------------------------------------
# DB adapter (same as run_pipeline.py but standalone)
# ---------------------------------------------------------------------------

class DB:
    """Async SQLite adapter that returns dicts and translates $N → ?."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @staticmethod
    def _translate(sql: str) -> str:
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
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    requirements TEXT,
    status TEXT DEFAULT 'draft',
    config_json TEXT DEFAULT '{}',
    created_at REAL,
    updated_at REAL,
    completed_at REAL,
    owner_id TEXT,
    repo_path TEXT,
    git_base_branch TEXT,
    git_project_branch TEXT,
    git_worktree_path TEXT,
    git_state_json TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    plan_id TEXT,
    title TEXT,
    description TEXT,
    task_type TEXT DEFAULT 'code',
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    model_tier TEXT DEFAULT 'claude_code',
    model_used TEXT,
    context_json TEXT DEFAULT '{}',
    tools_json TEXT,
    system_prompt TEXT,
    output_text TEXT,
    output_artifacts_json TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    max_tokens INTEGER,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    error TEXT,
    started_at REAL,
    completed_at REAL,
    created_at REAL,
    updated_at REAL,
    wave INTEGER DEFAULT 0,
    verification_status TEXT,
    verification_notes TEXT,
    requirement_ids_json TEXT,
    phase TEXT,
    git_branch TEXT,
    git_commit_sha TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    rationale TEXT,
    complexity TEXT DEFAULT 'medium',
    implementation_notes TEXT,
    test_strategy TEXT
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT,
    depends_on TEXT,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    plan_json TEXT,
    level TEXT DEFAULT 'L1',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS god_relay_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS god_registry (
    name TEXT PRIMARY KEY,
    last_seen_id INTEGER DEFAULT 0,
    last_heartbeat REAL
);
"""


# ---------------------------------------------------------------------------
# LLM Gateway call
# ---------------------------------------------------------------------------

async def _call_gateway(
    *,
    provider: str = "gemini",
    system_prompt: str,
    user_message: str,
    gateway_url: str = "http://localhost:5210",
    timeout: float = 120.0,
) -> dict:
    """Call the LLM Gateway."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": provider,
            "system_prompt": system_prompt,
            "user_message": user_message,
        })
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class HekateEngine:
    """Unified Hekate orchestration engine."""

    def __init__(self, db: DB, pipeline: Pipeline, hermes_runner=None):
        self.db = db
        self.pipeline = pipeline
        self.hermes_runner = hermes_runner
        self.running = False
        self._tick_task: asyncio.Task | None = None

    async def setup_db(self):
        """Create all tables if they don't exist."""
        for statement in _SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    await self.db.execute_write(stmt, ())
                except Exception:
                    pass  # Table already exists

    async def start(self):
        """Start the pipeline tick loop."""
        self.running = True
        self._tick_task = asyncio.create_task(self._tick_loop(), name="hekate-pipeline")
        logger.info("Hekate engine started")

    async def stop(self):
        """Stop the pipeline and hermes."""
        self.running = False
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        if self.hermes_runner:
            await self.hermes_runner.shutdown(timeout=10.0)
        logger.info("Hekate engine stopped")

    async def _tick_loop(self):
        """Background tick loop."""
        while self.running:
            try:
                await self.pipeline.tick()
            except Exception as e:
                logger.error("Pipeline tick error: %s", e)
            await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    async def create_project(
        self,
        name: str,
        requirements: str,
        config: dict | None = None,
        repo_path: str | None = None,
    ) -> str:
        """Create a project and emit project_created event."""
        project_id = uuid.uuid4().hex[:12]
        now = time.time()
        config_json = json.dumps(config or {})

        await self.db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, "
            "repo_path, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            (project_id, name, requirements, "draft", config_json,
             repo_path, now, now),
        )

        # Emit project_created event
        await self.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_created", "engine", json.dumps({"project_id": project_id}), "info", now),
        )

        logger.info("Created project %s: %s", project_id[:8], name)
        return project_id

    async def get_project_status(self, project_id: str) -> dict:
        """Get project with task breakdown."""
        project = await self.db.fetchone(
            "SELECT * FROM projects WHERE id = $1", (project_id,),
        )
        if not project:
            return {"error": "Project not found"}

        tasks = await self.db.fetchall(
            "SELECT id, title, status, wave, task_type, model_tier, retry_count, "
            "verification_status, cost_usd FROM tasks WHERE project_id = $1 ORDER BY wave",
            (project_id,),
        )

        project["tasks"] = tasks
        return project

    async def list_projects(self) -> list[dict]:
        """List all projects with summary."""
        return await self.db.fetchall(
            "SELECT id, name, status, created_at, updated_at FROM projects ORDER BY created_at DESC",
            (),
        )

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    async def stream_events(
        self,
        since_id: int = 0,
        project_id: str | None = None,
        max_events: int | None = None,
    ) -> AsyncIterator[dict]:
        """Stream relay events. Yields dicts with event data."""
        count = 0
        if project_id:
            rows = await self.db.fetchall(
                "SELECT id, event_type, source, payload, severity, created_at "
                "FROM god_relay_events WHERE id > $1 AND payload LIKE $2 "
                "ORDER BY id ASC",
                (since_id, f'%{project_id}%'),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT id, event_type, source, payload, severity, created_at "
                "FROM god_relay_events WHERE id > $1 ORDER BY id ASC",
                (since_id,),
            )

        for row in rows:
            yield row
            count += 1
            if max_events and count >= max_events:
                return

    # ------------------------------------------------------------------
    # Standalone planner (no monolith import)
    # ------------------------------------------------------------------

    async def generate_plan(
        self,
        requirements: str,
        project_name: str = "",
        provider: str = "gemini",
        level: str = "L1",
    ) -> dict:
        """Generate a plan via LLM gateway. No monolith imports."""
        system_prompt = (
            "You are a software project planner. Generate a structured plan as JSON.\n\n"
            "Output format:\n"
            '{"summary": "...", "phases": [{"name": "...", "tasks": ['
            '{"title": "...", "description": "...", "task_type": "code|research|test", '
            '"depends_on": []}]}]}\n\n'
            "Rules:\n"
            "- Break work into small, focused tasks (2-5 minutes each)\n"
            "- Group related tasks into phases\n"
            "- Set depends_on as array of task indices within the same phase\n"
            "- task_type: 'code' for implementation, 'research' for analysis, 'test' for testing\n"
        )

        user_message = f"Project: {project_name}\n\nRequirements:\n{requirements}"

        result = await _call_gateway(
            provider=provider,
            system_prompt=system_prompt,
            user_message=user_message,
        )

        text = result.get("text", "")

        from gods.providers.response_validator import extract_json
        plan = extract_json(text)
        if plan is None:
            raise ValueError(f"Could not parse plan from LLM response ({len(text)} chars)")

        return plan

    # ------------------------------------------------------------------
    # Standalone decomposer
    # ------------------------------------------------------------------

    async def decompose_plan(
        self,
        project_id: str,
        plan: dict,
        plan_id: str | None = None,
    ) -> int:
        """Decompose a plan into task rows. No monolith imports.

        Returns the number of tasks created.
        """
        if not plan_id:
            plan_id = uuid.uuid4().hex[:12]

        now = time.time()

        # Save plan
        await self.db.execute_write(
            "INSERT OR REPLACE INTO plans (id, project_id, plan_json, created_at) "
            "VALUES ($1, $2, $3, $4)",
            (plan_id, project_id, json.dumps(plan), now),
        )

        # Extract tasks from phases
        all_tasks: list[dict] = []
        wave = 0
        for phase in plan.get("phases", []):
            phase_tasks = phase.get("tasks", [])
            # Track index offset for dependency resolution
            offset = len(all_tasks)
            for i, task in enumerate(phase_tasks):
                task_id = uuid.uuid4().hex[:12]
                # Resolve depends_on indices to task IDs
                deps = []
                for dep_idx in task.get("depends_on", []):
                    if isinstance(dep_idx, int) and 0 <= dep_idx + offset < len(all_tasks):
                        deps.append(all_tasks[dep_idx + offset]["id"])
                    elif isinstance(dep_idx, int) and 0 <= dep_idx < len(all_tasks):
                        deps.append(all_tasks[dep_idx]["id"])

                status = "pending" if wave == 0 and not deps else "blocked"

                all_tasks.append({
                    "id": task_id,
                    "title": task.get("title", f"Task {i}"),
                    "description": task.get("description", ""),
                    "task_type": task.get("task_type", "code"),
                    "wave": wave,
                    "deps": deps,
                    "status": status,
                })
            wave += 1

        # Write task rows
        for task in all_tasks:
            await self.db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, "
                "task_type, wave, status, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                (task["id"], project_id, plan_id, task["title"],
                 task["description"], task["task_type"], task["wave"],
                 task["status"], now, now),
            )

            # Write deps
            for dep_id in task["deps"]:
                await self.db.execute_write(
                    "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
                    (task["id"], dep_id),
                )

        logger.info("Decomposed plan %s → %d tasks across %d waves",
                     plan_id[:8], len(all_tasks), wave)

        return len(all_tasks)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_engine(
    db_path: str = ":memory:",
    max_concurrent: int = 2,
) -> HekateEngine:
    """Create a HekateEngine with all handlers registered.

    For in-memory DB (testing), sets up synchronously.
    For file DB, caller must await engine.setup_db().
    """
    # Create connection synchronously for factory pattern
    # Actual async setup happens in setup_db()
    import sqlite3

    # Create pipeline with a placeholder DB — real DB set in setup_db
    pipeline = Pipeline(db=None, source_name="hekate")

    engine = HekateEngine(db=None, pipeline=pipeline)
    engine._db_path = db_path
    engine._max_concurrent = max_concurrent

    return engine


async def _init_engine(engine: HekateEngine):
    """Async initialization — called from setup_db or start."""
    conn = await aiosqlite.connect(engine._db_path)
    conn.row_factory = aiosqlite.Row
    db = DB(conn)

    engine.db = db
    engine.pipeline.db = db

    # Register handlers
    hermes = register_all_handlers(
        engine.pipeline,
        max_concurrent=engine._max_concurrent,
    )
    engine.hermes_runner = hermes
    hermes.db = db


# Patch create_engine to auto-init on setup_db
_original_setup = None


async def _setup_db_with_init(self):
    """Setup DB tables and initialize engine."""
    if self.db is None:
        await _init_engine(self)
    for statement in _SCHEMA_SQL.split(";"):
        stmt = statement.strip()
        if stmt:
            try:
                await self.db.execute_write(stmt, ())
            except Exception:
                pass


HekateEngine.setup_db = _setup_db_with_init
