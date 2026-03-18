#  Orchestration Engine - Odin Routes
#
#  Status and audit trail endpoints for the Odin system overseer.
#
#  Depends on: container.py, middleware/auth.py
#  Used by:    app.py

import logging

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, Query

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user

logger = logging.getLogger("orchestration.routes.odin")

router = APIRouter(prefix="/odin", tags=["odin"])


@router.get("/status")
@inject
async def odin_status(
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Get Odin's current status and recent activity."""
    from backend.container import container

    odin = container.odin()
    status = odin.get_status()

    # Add god health summary from god_events table (Postgres only)
    try:
        god_rows = await db.fetchall(
            "SELECT DISTINCT ON (god_name) god_name, payload, created_at "
            "FROM god_events WHERE event_type = 'heartbeat' "
            "ORDER BY god_name, created_at DESC",
            (),
        )
        status["gods"] = [
            {"name": r["god_name"], "last_heartbeat": str(r["created_at"])}
            for r in god_rows
        ]
    except Exception:
        # god_events table may not exist yet (migration 025), or SQLite backend
        status["gods"] = []

    return status


@router.get("/decisions")
@inject
async def odin_decisions(
    limit: int = Query(50, ge=1, le=500),
    project_id: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Get Odin's decision audit trail.

    Queries odin_decisions (post-migration 024) with fallback to
    sentinel_decisions (pre-migration) for backwards compatibility.
    """
    rows = []

    # Try odin_decisions first (post-migration 024 schema)
    try:
        if project_id:
            rows = await db.fetchall(
                "SELECT decision_id, project_id, task_id, decision_type, "
                "reasoning, confidence, action_taken, outcome, created_at, details_json "
                "FROM odin_decisions WHERE project_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                (project_id, limit),
            )
        else:
            rows = await db.fetchall(
                "SELECT decision_id, project_id, task_id, decision_type, "
                "reasoning, confidence, action_taken, outcome, created_at, details_json "
                "FROM odin_decisions ORDER BY created_at DESC LIMIT $1",
                (limit,),
            )
    except Exception:
        rows = []

    # Fallback to sentinel_decisions (pre-migration 024)
    if not rows:
        try:
            if project_id:
                rows = await db.fetchall(
                    "SELECT id, project_id, command, reasoning, confidence, "
                    "outcome, timestamp, details_json "
                    "FROM sentinel_decisions WHERE project_id = $1 "
                    "ORDER BY timestamp DESC LIMIT $2",
                    (project_id, limit),
                )
            else:
                rows = await db.fetchall(
                    "SELECT id, project_id, command, reasoning, confidence, "
                    "outcome, timestamp, details_json "
                    "FROM sentinel_decisions ORDER BY timestamp DESC LIMIT $1",
                    (limit,),
                )
        except Exception:
            rows = []

    return [dict(r) for r in rows]


@router.get("/config")
@inject
async def odin_config(
    current_user: dict = Depends(get_current_user),
):
    """Get Odin's runtime configuration."""
    from backend.config import cfg

    return {
        "enabled": cfg("odin.enabled", False),
        "dispatch_enabled": cfg("odin.dispatch_enabled", False),
        "provider": cfg("odin.provider", "claude"),
        "model": cfg("odin.model", "sonnet"),
        "tick_interval": int(cfg("odin.tick_interval", 30)),
        "staleness_seconds": int(cfg("odin.staleness_seconds", 300)),
    }
