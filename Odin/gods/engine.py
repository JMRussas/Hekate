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

    def __init__(self, db: DB, pipeline: Pipeline, hermes_runner=None, mimir_runner=None):
        self.db = db
        self.pipeline = pipeline
        self.hermes_runner = hermes_runner
        self.mimir_runner = mimir_runner
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

    async def recover_stuck_tasks(self):
        """Reset tasks/projects stuck from a previous crash.

        - Tasks in 'running' → reset to 'pending' (increment retry_count)
        - Projects in 'planning' → reset to 'draft'
        """
        # Reset stuck running tasks
        stuck_tasks = await self.db.fetchall(
            "SELECT id, title FROM tasks WHERE status = $1", ("running",),
        )
        for t in stuck_tasks:
            tid = t["id"] if isinstance(t, dict) else t[0]
            title = t["title"] if isinstance(t, dict) else t[1]
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                "updated_at = $2 WHERE id = $3",
                ("pending", time.time(), tid),
            )
            logger.info("Recovery: reset stuck task %s (%s) to pending", tid[:8], title[:30])

        # Reset stuck planning projects
        stuck_projects = await self.db.fetchall(
            "SELECT id, name FROM projects WHERE status = $1", ("planning",),
        )
        for p in stuck_projects:
            pid = p["id"] if isinstance(p, dict) else p[0]
            name = p["name"] if isinstance(p, dict) else p[1]
            await self.db.execute_write(
                "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                ("draft", time.time(), pid),
            )
            logger.info("Recovery: reset stuck project %s (%s) to draft", pid[:8], name[:30])

        if stuck_tasks or stuck_projects:
            logger.info("Recovery: reset %d tasks + %d projects",
                        len(stuck_tasks), len(stuck_projects))

    async def start(self):
        """Start the pipeline tick loop."""
        await self.recover_stuck_tasks()
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
        if self.mimir_runner:
            await self.mimir_runner.shutdown(timeout=10.0)
        logger.info("Hekate engine stopped")

    async def _tick_loop(self):
        """Background tick loop. Never crashes — logs errors and continues.

        Every 12 ticks (~60s) injects project_tick events for all executing
        projects as a safety net. This ensures dispatch re-runs even if the
        event chain breaks.
        """
        consecutive_errors = 0
        tick_count = 0
        while self.running:
            try:
                await self.pipeline.tick()
                consecutive_errors = 0

                # Safety net: inject project_ticks every ~60s
                tick_count += 1
                if tick_count % 12 == 0:
                    try:
                        rows = await self.db.fetchall(
                            "SELECT id FROM projects WHERE status = $1",
                            ("executing",),
                        )
                        for row in rows:
                            pid = row["id"] if isinstance(row, dict) else row[0]
                            await self.db.execute_write(
                                "INSERT INTO god_relay_events "
                                "(event_type, source, payload, severity, created_at) "
                                "VALUES ($1, $2, $3, $4, $5)",
                                ("heartbeat_tick", "heartbeat",
                                 json.dumps({"project_id": pid}),
                                 "info", time.time()),
                            )
                    except Exception:
                        pass  # Non-critical

            except Exception as e:
                consecutive_errors += 1
                logger.error("Pipeline tick error (#%d): %s: %s",
                             consecutive_errors, type(e).__name__, e)
                if consecutive_errors >= 10:
                    logger.error("Pipeline: 10 consecutive errors — backing off to 30s")
                    await asyncio.sleep(30.0)
                    consecutive_errors = 0
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
        plan_config: dict | None = None,
    ) -> int:
        """Decompose a plan into task rows. No monolith imports.

        Attaches Conductor-style TaskDefinition to each task's context_json
        based on task_type and complexity. If the task dict contains an explicit
        "task_definition" key (from L3+ planning), that overrides defaults.

        Returns the number of tasks created.
        """
        from gods.task_definition import TaskDefinition, apply_defaults, merge_with_plan_config

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

                # Build TaskDefinition: explicit override or type-based defaults
                if "task_definition" in task:
                    td = TaskDefinition.from_dict(task["task_definition"])
                else:
                    td = apply_defaults(
                        task.get("task_type", "code"),
                        task.get("complexity", "medium"),
                    )

                # Merge with plan-level config if present
                td = merge_with_plan_config(td, plan_config)

                # Build context_json with task_definition embedded
                context = {"task_definition": td.to_dict()}

                all_tasks.append({
                    "id": task_id,
                    "title": task.get("title", f"Task {i}"),
                    "description": task.get("description", ""),
                    "task_type": task.get("task_type", "code"),
                    "wave": wave,
                    "deps": deps,
                    "status": status,
                    "context_json": json.dumps(context),
                })
            wave += 1

        # Write task rows
        for task in all_tasks:
            await self.db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, "
                "task_type, wave, status, context_json, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                (task["id"], project_id, plan_id, task["title"],
                 task["description"], task["task_type"], task["wave"],
                 task["status"], task["context_json"], now, now),
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
    hermes, mimir = register_all_handlers(
        engine.pipeline,
        max_concurrent=engine._max_concurrent,
    )
    engine.hermes_runner = hermes
    engine.mimir_runner = mimir
    hermes.db = db
    mimir.db = db


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

    # Verify key tables exist
    expected_tables = ["projects", "tasks", "task_deps", "plans", "god_relay_events", "god_registry"]
    for table_name in expected_tables:
        row = await self.db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=$1",
            (table_name,),
        )
        if not row:
            logger.warning("Schema validation: expected table '%s' is missing after setup", table_name)


HekateEngine.setup_db = _setup_db_with_init
