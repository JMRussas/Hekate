"""Hekate API — FastAPI app wrapping the engine.

Serves the dashboard routes. No monolith imports.
Replaces orchestration/backend/app.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import uuid

from gods.engine import HekateEngine, create_engine, _init_engine
from gods import safe_json
from gods.task_states import transition_task

logger = logging.getLogger("gods.api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _checkpoint_row_to_dict(row) -> dict:
    """Convert a checkpoint DB row to the dict shape the dashboard expects."""
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "task_id": row.get("task_id"),
        "checkpoint_type": row["checkpoint_type"],
        "summary": row["summary"],
        "attempts": json.loads(row["attempts_json"]) if row.get("attempts_json") else [],
        "question": row["question"],
        "schema_json": json.loads(row["schema_json"]) if row.get("schema_json") else None,
        "response": row.get("response"),
        "resolved_at": row.get("resolved_at"),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str
    requirements: str
    planning_rigor: str = "L2"
    repo_path: str | None = None
    additional_repos: list[str] | None = None
    config: dict = {}


class DecomposeRequest(BaseModel):
    phases: list[dict]


class ExecuteRequest(BaseModel):
    pass


class TaskActionRequest(BaseModel):
    action: str = ""
    feedback: str = ""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    db_path: str | None = None,
    dsn: str | None = None,
    max_concurrent: int = 4,
    frontend_dist: str | None = None,
) -> FastAPI:
    """Create the Hekate FastAPI app.

    Args:
        db_path: SQLite database path (fallback if no DSN)
        dsn: Postgres connection string (takes precedence)
        max_concurrent: Max parallel CLI tasks for hermes
        frontend_dist: Path to frontend/dist/ for static serving
    """
    # Postgres DSN from env takes priority
    if dsn is None:
        dsn = os.environ.get("ORCHESTRATION_DSN")
    if db_path is None and dsn is None:
        db_path = os.environ.get(
            "ORCHESTRATION_DB",
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "orchestration", "data", "orchestration.db"),
        )

    engine = create_engine(db_path=db_path, dsn=dsn, max_concurrent=max_concurrent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start engine + pipeline on startup, stop on shutdown."""
        await engine.setup_db()
        await engine.start()
        logger.info("Hekate engine started (pipeline running)")
        yield
        await engine.stop()
        logger.info("Hekate engine stopped")

    app = FastAPI(
        title="Hekate",
        description="AI orchestration engine",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store engine on app state for route access
    app.state.engine = engine

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    @app.post("/api/projects")
    async def create_project(req: CreateProjectRequest):
        e: HekateEngine = app.state.engine

        # Translate monolith config fields to engine config
        config = dict(req.config) if req.config else {}
        if "planning_rigor" in config or hasattr(req, "planning_rigor"):
            rigor = config.pop("planning_rigor", getattr(req, "planning_rigor", "L2"))
            config.setdefault("target_level", rigor)
        if "execution_mode" in config:
            config.pop("execution_mode")  # Not used by engine
        config.setdefault("tdd", True)
        config.setdefault("narration", True)

        # Require repo_path for code projects — warn if missing
        repo_path = req.repo_path
        if not repo_path:
            logger.warning("Project '%s' created without repo_path — CLI will run in default directory", req.name)

        project_id = await e.create_project(
            name=req.name,
            requirements=req.requirements,
            config=config,
            repo_path=repo_path,
            additional_repos=req.additional_repos,
        )
        return await e.get_project_status(project_id)

    @app.get("/api/projects")
    async def list_projects(status: str | None = None):
        e: HekateEngine = app.state.engine
        projects = await e.list_projects()
        if status:
            projects = [p for p in projects if p.get("status") == status]
        return projects

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        e: HekateEngine = app.state.engine
        result = await e.get_project_status(project_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.post("/api/projects/{project_id}/execute")
    async def start_execution(project_id: str):
        e: HekateEngine = app.state.engine
        # Emit project_created event to trigger the pipeline
        await e.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_created", "api",
             json.dumps({"project_id": project_id}),
             "info", time.time()),
        )
        return {"status": "executing", "project_id": project_id}

    @app.post("/api/projects/{project_id}/decompose")
    async def decompose_plan(project_id: str, req: DecomposeRequest):
        e: HekateEngine = app.state.engine
        plan = {"phases": req.phases}
        count = await e.decompose_plan(project_id, plan)
        return {"tasks_created": count}

    @app.post("/api/projects/{project_id}/cancel")
    async def cancel_project(project_id: str):
        e: HekateEngine = app.state.engine
        await e.db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("cancelled", time.time(), project_id),
        )
        return {"status": "cancelled"}

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @app.get("/api/tasks/project/{project_id}")
    async def list_tasks(project_id: str, status: str | None = None, wave: int | None = None):
        """List tasks for a project.

        Includes the per-task dependency list (joined from `task_deps`) so
        downstream consumers can render the wave DAG without an N+1 fetch.
        Also surfaces phase, priority, parsed tools, output_text, error,
        started_at, and completed_at — fields the TS Task type already
        expects but the previous slim SELECT omitted.
        """
        e: HekateEngine = app.state.engine
        sql = (
            "SELECT id, title, description, task_type, status, wave, model_tier, "
            "retry_count, verification_status, cost_usd, created_at, updated_at, "
            "phase, priority, tools_json, output_text, error, started_at, completed_at "
            "FROM tasks WHERE project_id = $1"
        )
        params: list = [project_id]
        if status:
            sql += f" AND status = ${len(params) + 1}"
            params.append(status)
        if wave is not None:
            sql += f" AND wave = ${len(params) + 1}"
            params.append(wave)
        sql += " ORDER BY wave, priority"

        rows = await e.db.fetchall(sql, tuple(params))
        if not rows:
            return rows

        # Fetch dependencies for these tasks in a single follow-up query.
        # Two queries instead of a JOIN+GROUP keeps the SQL cross-backend
        # compatible (PG asyncpg in prod, sqlite in tests; neither has a
        # cleanly portable array_agg).
        task_ids = [r["id"] for r in rows]
        dep_placeholders = ",".join(f"${i + 1}" for i in range(len(task_ids)))
        dep_sql = (
            f"SELECT task_id, depends_on FROM task_deps "
            f"WHERE task_id IN ({dep_placeholders})"
        )
        dep_rows = await e.db.fetchall(dep_sql, tuple(task_ids))
        deps_by_task: dict[str, list[str]] = {}
        for d in dep_rows:
            deps_by_task.setdefault(d["task_id"], []).append(d["depends_on"])

        # Parse tools_json once + attach depends_on. The frontend wants
        # `tools: string[]`, not the raw stored JSON string.
        for r in rows:
            r["depends_on"] = deps_by_task.get(r["id"], [])
            tools_raw = r.pop("tools_json", None)
            try:
                r["tools"] = json.loads(tools_raw) if tools_raw else []
            except (ValueError, TypeError):
                r["tools"] = []

        return rows
    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        e: HekateEngine = app.state.engine
        row = await e.db.fetchone(
            "SELECT * FROM tasks WHERE id = $1", (task_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        return row

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str):
        e: HekateEngine = app.state.engine
        await e.db.execute_write(
            "UPDATE tasks SET status = $1, error = NULL, output_text = NULL, "
            "retry_count = retry_count + 1, updated_at = $2 WHERE id = $3",
            ("pending", time.time(), task_id),
        )
        row = await e.db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
        return row or {"id": task_id, "status": "pending"}

    @app.post("/api/tasks/{task_id}/verify")
    async def verify_task(task_id: str, req: Request):
        """Submit verification verdict — called by Mimir agent via prometheus MCP.

        Emits relay events so odin_lifecycle can unblock dependents and
        progress waves. This is the authoritative source of task_verified
        events — mimir's _agent_submitted path relies on reading the DB
        state set here.
        """
        e: HekateEngine = app.state.engine
        body = await req.json()
        verdict = body.get("verdict", "human_needed")
        feedback = body.get("feedback", "")
        confidence = body.get("confidence", 1.0)

        row = await e.db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
        if not row:
            raise HTTPException(404, f"Task {task_id} not found")

        project_id = row.get("project_id")
        max_retries = row.get("max_retries") or 3

        if verdict == "passed":
            await e.db.execute_write(
                "UPDATE tasks SET verification_status = $1, verification_notes = $2, updated_at = $3 WHERE id = $4",
                ("passed", feedback[:500] if feedback else None, time.time(), task_id),
            )
            # Emit task_verified — drives wave progression via odin_lifecycle
            if project_id:
                await e.db.execute_write(
                    "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    ("task_verified", "verify_api", json.dumps({
                        "task_id": task_id, "project_id": project_id, "confidence": confidence,
                    }), "info", time.time()),
                )
            return {"accepted": True, "message": "Task verified as passed."}
        elif verdict == "gaps_found":
            retry_count = row.get("retry_count") or 0
            if retry_count < max_retries:
                await e.db.execute_write(
                    "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, "
                    "retry_count = retry_count + 1, updated_at = $4 WHERE id = $5",
                    ("pending", "gaps_found", feedback[:500], time.time(), task_id),
                )
                # Emit project_tick so odin re-dispatches the retried task
                if project_id:
                    await e.db.execute_write(
                        "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        ("project_tick", "verify_api", json.dumps({
                            "project_id": project_id,
                        }), "info", time.time()),
                    )
                return {"accepted": True, "message": "Task will be retried with feedback."}
            else:
                await e.db.execute_write(
                    "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, updated_at = $4 WHERE id = $5",
                    ("needs_review", "gaps_found", feedback[:500], time.time(), task_id),
                )
                return {"accepted": True, "message": "Max retries reached, needs human review."}
        else:
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, updated_at = $4 WHERE id = $5",
                ("needs_review", "human_needed", feedback[:500], time.time(), task_id),
            )
            return {"accepted": True, "message": "Flagged for human review."}

    @app.post("/api/tasks/{task_id}/review")
    async def review_task(task_id: str, req: TaskActionRequest):
        e: HekateEngine = app.state.engine
        # Get project_id for relay event
        task_row = await e.db.fetchone("SELECT project_id FROM tasks WHERE id = $1", (task_id,))
        project_id = task_row["project_id"] if task_row else None

        if req.action == "approve":
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, verification_status = $2, updated_at = $3 WHERE id = $4",
                ("completed", "passed", time.time(), task_id),
            )
            # Emit task_verified so odin_lifecycle unblocks dependents
            if project_id:
                await e.db.execute_write(
                    "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    ("task_verified", "api", json.dumps({
                        "task_id": task_id, "project_id": project_id, "confidence": 1.0,
                    }), "info", time.time()),
                )
        elif req.action == "reject":
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                "error = $2, updated_at = $3 WHERE id = $4",
                ("pending", req.feedback or "Rejected by reviewer", time.time(), task_id),
            )
            # Emit project_tick so odin re-dispatches
            if project_id:
                await e.db.execute_write(
                    "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    ("project_tick", "api", json.dumps({"project_id": project_id}), "info", time.time()),
                )
        row = await e.db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
        return row or {"id": task_id}

    # ------------------------------------------------------------------
    # Events (for SSE / polling)
    # ------------------------------------------------------------------

    @app.get("/api/events/stream")
    async def stream_all_events(since: int = 0, limit: int = 50):
        """Poll for new events across all projects."""
        e: HekateEngine = app.state.engine
        events = []
        async for event in e.stream_events(since_id=since, max_events=limit):
            events.append(event)
        return events

    @app.get("/api/events/{project_id}")
    async def get_events(project_id: str, since: int = 0, limit: int = 100):
        e: HekateEngine = app.state.engine
        events = []
        async for event in e.stream_events(
            since_id=since, project_id=project_id, max_events=limit,
        ):
            events.append(event)
        return events

    # ------------------------------------------------------------------
    # Observatory — pipeline observability
    # ------------------------------------------------------------------

    @app.get("/api/observatory/{project_id}/stream")
    async def observatory_sse(project_id: str, since: int = 0):
        """True SSE stream for Pipeline Observatory."""
        from fastapi.responses import StreamingResponse

        e: HekateEngine = app.state.engine

        async def event_generator():
            cursor = since
            while True:
                events = []
                async for ev in e.stream_events(
                    since_id=cursor, project_id=project_id, max_events=50,
                ):
                    events.append(ev)
                for ev in events:
                    cursor = max(cursor, ev.get("id", cursor))
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                if not events:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/observatory/task/{task_id}/timeline")
    async def task_timeline(task_id: str, since: int = 0, limit: int = 200):
        """All relay events for a specific task, chronologically."""
        e: HekateEngine = app.state.engine
        rows = await e.db.fetchall(
            "SELECT id, event_type, source, payload, severity, created_at "
            "FROM god_relay_events "
            "WHERE id > $1 AND payload LIKE $2 "
            "ORDER BY id ASC LIMIT $3",
            (since, f'%{task_id}%', limit),
        )
        return [dict(r) for r in (rows or [])]

    @app.get("/api/observatory/{project_id}/summary")
    async def event_summary(project_id: str):
        """Count of events by type for a project."""
        e: HekateEngine = app.state.engine
        rows = await e.db.fetchall(
            "SELECT event_type, COUNT(*) AS cnt "
            "FROM god_relay_events "
            "WHERE payload LIKE $1 "
            "GROUP BY event_type ORDER BY cnt DESC",
            (f'%{project_id}%',),
        )
        return [dict(r) for r in (rows or [])]

    @app.get("/api/observatory/handlers")
    async def list_handlers():
        """List all registered pipeline handlers and their configurations."""
        e: HekateEngine = app.state.engine
        handlers = []
        for reg in e.pipeline._handlers:
            handlers.append({
                "name": reg.name,
                "event_type": reg.event_type,
                "has_gate": reg.gate is not None,
                "max_retries": reg.max_retries,
            })
        return handlers

    # ------------------------------------------------------------------
    # Services (provider status)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    @app.get("/api/usage/rate-limits")
    async def get_rate_limits():
        """Per-provider rate limit status + recent rate_limit_hit events."""
        from gods.handlers.registration import _rate_limiter

        e: HekateEngine = app.state.engine
        providers = _rate_limiter.status()

        # Recent rate-limit gate failures (last hour)
        # The rate gate emits gate_failed events with handler=tyche_rate_gate
        one_hour_ago = time.time() - 3600
        rows = await e.db.fetchall(
            "SELECT id, event_type, source, payload, severity, created_at "
            "FROM god_relay_events "
            "WHERE event_type = $1 AND created_at > $2 "
            "ORDER BY created_at DESC LIMIT 50",
            ("gate_failed", one_hour_ago),
        )
        recent_hits = []
        for r in rows:
            hit = dict(r)
            if isinstance(hit.get("payload"), str):
                try:
                    hit["payload"] = json.loads(hit["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            # Filter to only rate-gate failures
            payload = hit.get("payload") or {}
            if isinstance(payload, dict) and payload.get("handler") == "tyche_rate_gate":
                recent_hits.append(hit)

        return {"providers": providers, "recent_hits": recent_hits}

    @app.get("/api/usage/budget")
    async def get_budget():
        return {
            "daily_limit_usd": 5.0, "daily_spent_usd": 0.0, "daily_pct": 0.0,
            "monthly_limit_usd": 50.0, "monthly_spent_usd": 0.0, "monthly_pct": 0.0,
        }

    @app.get("/api/usage/summary")
    async def get_usage_summary():
        return {"total_cost": 0.0, "total_tasks": 0, "by_tier": {}}

    @app.get("/api/usage/daily")
    async def get_daily_usage():
        return []

    @app.get("/api/usage/by-project")
    async def get_usage_by_project():
        return []

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    @app.get("/api/services")
    async def list_services():
        """Check which providers/services are available."""
        import asyncio
        import httpx
        services = []
        from gods.config import GATEWAY_URL, CONTEXT_STORE_URL, MCP_URL
        checks = [
            ("LLM Gateway", f"{GATEWAY_URL}/health"),
            ("Context Store", f"{CONTEXT_STORE_URL}/health"),
            ("Hekate MCP", f"{MCP_URL}/health"),
        ]
        for name, url in checks:
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get(url)
                    services.append({"name": name, "status": "running" if r.status_code < 500 else "error", "url": url})
            except httpx.ConnectError:
                logger.debug("Service %s unreachable (connect error)", name)
                services.append({"name": name, "status": "stopped", "url": url})
            except asyncio.TimeoutError:
                logger.debug("Service %s unreachable (timeout)", name)
                services.append({"name": name, "status": "stopped", "url": url})
            except Exception as e:
                logger.debug("Service %s unreachable: %s", name, e)
                services.append({"name": name, "status": "stopped", "url": url})
        return services

    # ------------------------------------------------------------------
    # Catch-all routes the dashboard expects but we haven't implemented
    # ------------------------------------------------------------------

    @app.post("/api/projects/{project_id}/plan")
    async def generate_plan_stub(project_id: str):
        """Stub — planning happens automatically via pipeline."""
        return {"message": "Planning is automatic. Use /execute to trigger."}

    @app.get("/api/projects/{project_id}/plans")
    async def list_plans(project_id: str):
        e: HekateEngine = app.state.engine
        plans = await e.db.fetchall(
            "SELECT id, project_id, plan_json, level, created_at "
            "FROM plans WHERE project_id = $1 ORDER BY created_at DESC",
            (project_id,),
        )
        return plans or []

    @app.post("/api/projects/{project_id}/plans/{plan_id}/approve")
    async def approve_plan(project_id: str, plan_id: str):
        """Approve a plan and trigger execution."""
        e: HekateEngine = app.state.engine

        # Update plan status
        await e.db.execute_write(
            "UPDATE plans SET status = $1 WHERE id = $2 AND project_id = $3",
            ("approved", plan_id, project_id),
        )

        # Get plan JSON for decomposition
        plan_row = await e.db.fetchone(
            "SELECT plan_json FROM plans WHERE id = $1", (plan_id,),
        )
        if not plan_row:
            raise HTTPException(status_code=404, detail="Plan not found")

        plan = json.loads(plan_row["plan_json"]) if isinstance(plan_row["plan_json"], str) else plan_row["plan_json"]

        # Decompose into tasks
        task_count = await e.decompose_plan(project_id, plan, plan_id=plan_id)

        # Emit project_created to trigger pipeline execution
        await e.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_created", "api",
             json.dumps({"project_id": project_id}),
             "info", time.time()),
        )

        return {"tasks_created": task_count, "plan_id": plan_id, "status": "approved"}

    @app.get("/api/projects/{project_id}/coverage")
    async def get_coverage(project_id: str):
        return {"total": 0, "covered": 0, "uncovered": []}

    @app.get("/api/checkpoints/schemas")
    async def list_checkpoint_schemas():
        """Return predefined checkpoint response schemas."""
        return {
            "approve_reject": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["approve", "reject"]},
                    "reason": {"type": "string", "maxLength": 10000},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "select_option": {
                "type": "object",
                "properties": {
                    "selected": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "maxLength": 10000},
                },
                "required": ["selected"],
                "additionalProperties": False,
            },
            "provide_file_path": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "Absolute file path"},
                    "description": {"type": "string", "maxLength": 10000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "free_text_with_reason": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["text", "reason", "confidence"],
                "additionalProperties": False,
            },
        }

    @app.get("/api/checkpoints/project/{project_id}")
    async def list_checkpoints(project_id: str, resolved: bool = False):
        e: HekateEngine = app.state.engine
        if resolved:
            rows = await e.db.fetchall(
                "SELECT * FROM checkpoints WHERE project_id = $1 "
                "ORDER BY created_at DESC",
                (project_id,),
            )
        else:
            rows = await e.db.fetchall(
                "SELECT * FROM checkpoints WHERE project_id = $1 AND resolved_at IS NULL "
                "ORDER BY created_at DESC",
                (project_id,),
            )
        return [_checkpoint_row_to_dict(r) for r in rows]

    @app.get("/api/checkpoints/{checkpoint_id}")
    async def get_checkpoint(checkpoint_id: str):
        e: HekateEngine = app.state.engine
        row = await e.db.fetchone(
            "SELECT * FROM checkpoints WHERE id = $1", (checkpoint_id,),
        )
        if not row:
            raise HTTPException(404, f"Checkpoint {checkpoint_id} not found")
        return _checkpoint_row_to_dict(row)

    @app.post("/api/checkpoints/{checkpoint_id}/resolve")
    async def resolve_checkpoint(checkpoint_id: str, req: Request):
        """Resolve a checkpoint: retry, skip, or fail the associated task."""
        e: HekateEngine = app.state.engine
        body = await req.json()
        action = body.get("action", "retry")
        guidance = body.get("guidance", "")

        row = await e.db.fetchone(
            "SELECT * FROM checkpoints WHERE id = $1", (checkpoint_id,),
        )
        if not row:
            raise HTTPException(404, f"Checkpoint {checkpoint_id} not found")
        if row.get("resolved_at") is not None:
            raise HTTPException(400, "Checkpoint already resolved")

        task_id = row.get("task_id")
        project_id = row.get("project_id")
        now = time.time()

        if action == "retry" and task_id:
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, error = NULL, output_text = NULL, "
                "retry_count = 0, updated_at = $2 WHERE id = $3",
                ("pending", now, task_id),
            )
            if project_id:
                await e.db.execute_write(
                    "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    ("project_tick", "checkpoint_resolve", json.dumps({"project_id": project_id}), "info", now),
                )
        elif action == "skip" and task_id:
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("cancelled", now, task_id),
            )
            if project_id:
                await e.db.execute_write(
                    "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    ("task_verified", "checkpoint_resolve", json.dumps({
                        "task_id": task_id, "project_id": project_id, "confidence": 1.0,
                    }), "info", now),
                )
        elif action == "fail" and task_id:
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("failed", now, task_id),
            )

        # Mark checkpoint resolved
        response_text = f"Action: {action}"
        if guidance:
            response_text += f" | Guidance: {guidance}"
        await e.db.execute_write(
            "UPDATE checkpoints SET response = $1, resolved_at = $2 WHERE id = $3",
            (response_text, now, checkpoint_id),
        )

        updated = await e.db.fetchone(
            "SELECT * FROM checkpoints WHERE id = $1", (checkpoint_id,),
        )
        return _checkpoint_row_to_dict(updated)

    @app.get("/api/projects/{project_id}/git-status")
    async def get_git_status(project_id: str):
        return None

    # ------------------------------------------------------------------
    # Feature flags — hot-toggle handlers without restart
    # ------------------------------------------------------------------

    @app.get("/api/flags")
    async def get_flags():
        from gods.flags import get_all
        return get_all()

    @app.post("/api/flags/{flag_name}")
    async def set_flag(flag_name: str, request: Request):
        from gods.flags import set_flag as _set_flag, is_enabled
        body = await request.json()
        value = body.get("enabled", not is_enabled(flag_name))
        _set_flag(flag_name, value)
        return {"flag": flag_name, "enabled": value}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def catch_all(path: str):
        """Catch unimplemented routes — return 501 instead of crashing."""
        return JSONResponse(
            status_code=501,
            content={"detail": f"Not implemented: /api/{path}"},
        )

    # ------------------------------------------------------------------
    # Static files (frontend dist/)
    # ------------------------------------------------------------------

    if frontend_dist:
        if os.path.isdir(frontend_dist):
            app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
        else:
            logger.warning("Frontend dist not found at %s — API only mode", frontend_dist)

    return app
