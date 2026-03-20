"""Hekate API — FastAPI app wrapping the engine.

Serves the dashboard routes. No monolith imports.
Replaces orchestration/backend/app.py.
"""

from __future__ import annotations

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

from gods.engine import HekateEngine, create_engine, _init_engine

logger = logging.getLogger("gods.api")


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str
    requirements: str
    planning_rigor: str = "L2"
    repo_path: str | None = None
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
    max_concurrent: int = 2,
    frontend_dist: str | None = None,
) -> FastAPI:
    """Create the Hekate FastAPI app.

    Args:
        db_path: SQLite database path. Defaults to env or orchestration/data/orchestration.db
        max_concurrent: Max parallel CLI tasks for hermes
        frontend_dist: Path to frontend/dist/ for static serving
    """
    if db_path is None:
        db_path = os.environ.get(
            "ORCHESTRATION_DB",
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "orchestration", "data", "orchestration.db"),
        )

    engine = create_engine(db_path=db_path, max_concurrent=max_concurrent)

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
        project_id = await e.create_project(
            name=req.name,
            requirements=req.requirements,
            config=req.config,
            repo_path=req.repo_path,
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
        e: HekateEngine = app.state.engine
        sql = (
            "SELECT id, title, description, task_type, status, wave, model_tier, "
            "retry_count, verification_status, cost_usd, created_at, updated_at "
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

        return await e.db.fetchall(sql, tuple(params))

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

    @app.post("/api/tasks/{task_id}/review")
    async def review_task(task_id: str, req: TaskActionRequest):
        e: HekateEngine = app.state.engine
        if req.action == "approve":
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, verification_status = $2, updated_at = $3 WHERE id = $4",
                ("completed", "passed", time.time(), task_id),
            )
        elif req.action == "reject":
            await e.db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                "error = $2, updated_at = $3 WHERE id = $4",
                ("pending", req.feedback or "Rejected by reviewer", time.time(), task_id),
            )
        row = await e.db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
        return row or {"id": task_id}

    # ------------------------------------------------------------------
    # Events (for SSE / polling)
    # ------------------------------------------------------------------

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
    # Services (provider status)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Usage (stubs — return empty data so dashboard doesn't 404)
    # ------------------------------------------------------------------

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
        import httpx
        services = []
        checks = [
            ("LLM Gateway", "http://localhost:5210/health"),
            ("Context Store", "http://localhost:5102/health"),
            ("Hekate MCP", "http://localhost:5110/health"),
        ]
        for name, url in checks:
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get(url)
                    services.append({"name": name, "status": "running" if r.status_code < 500 else "error", "url": url})
            except Exception:
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
            "SELECT id, project_id, version, model_used, cost_usd, plan_json, status, created_at "
            "FROM plans WHERE project_id = $1 ORDER BY created_at DESC",
            (project_id,),
        )
        return plans or []

    @app.get("/api/projects/{project_id}/coverage")
    async def get_coverage(project_id: str):
        return {"total": 0, "covered": 0, "uncovered": []}

    @app.get("/api/checkpoints/project/{project_id}")
    async def list_checkpoints(project_id: str):
        return []

    @app.get("/api/projects/{project_id}/git-status")
    async def get_git_status(project_id: str):
        return None

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
