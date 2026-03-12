#  Orchestration Engine - Checkpoint Routes
#
#  Endpoints for listing, viewing, and resolving checkpoints.
#  Checkpoints are created when tasks exhaust retries and need human input.
#
#  Depends on: container.py, models/schemas.py, middleware/auth.py
#  Used by:    app.py

import json
import time

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.config import DIAGNOSTIC_RAG_ENABLED
from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.models.enums import TaskStatus
from backend.models.schemas import CheckpointOut, CheckpointResolve
from backend.services.diagnostic_ingest import DiagnosticIngester

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _verify_checkpoint_ownership(db: Database, checkpoint_id: str, user: dict):
    """Fetch a checkpoint and verify the user owns its project."""
    row = await db.fetchone("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
    if not row:
        raise HTTPException(404, f"Checkpoint {checkpoint_id} not found")
    project = await db.fetchone("SELECT owner_id FROM projects WHERE id = ?", (row["project_id"],))
    if not project or (user.get("role") != "admin" and project["owner_id"] != user["id"]):
        raise HTTPException(403, "You do not own this checkpoint's project")
    return row


def _row_to_checkpoint(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "checkpoint_type": row["checkpoint_type"],
        "summary": row["summary"],
        "attempts": json.loads(row["attempts_json"]) if row["attempts_json"] else [],
        "question": row["question"],
        "response": row["response"],
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/project/{project_id}")
@inject
async def list_checkpoints(
    project_id: str,
    resolved: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[CheckpointOut]:
    """List checkpoints for a project. By default shows unresolved only."""
    from backend.routes.projects import _get_owned_project
    await _get_owned_project(db, project_id, current_user)

    if resolved:
        rows = await db.fetchall(
            "SELECT * FROM checkpoints WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM checkpoints WHERE project_id = ? AND resolved_at IS NULL "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )

    return [CheckpointOut(**_row_to_checkpoint(r)) for r in rows]


@router.get("/{checkpoint_id}")
@inject
async def get_checkpoint(
    checkpoint_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> CheckpointOut:
    """Get checkpoint details."""
    row = await _verify_checkpoint_ownership(db, checkpoint_id, current_user)
    return CheckpointOut(**_row_to_checkpoint(row))


@router.post("/{checkpoint_id}/resolve")
@inject
async def resolve_checkpoint(
    checkpoint_id: str,
    body: CheckpointResolve,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
    diagnostic_ingester: DiagnosticIngester = Depends(Provide[Container.diagnostic_ingester]),
) -> CheckpointOut:
    """Resolve a checkpoint with user action.

    Actions:
        retry — reset the task to PENDING with user guidance in context.
        skip — cancel the task, allowing the project to continue.
        fail — mark the task as FAILED.
    """
    row = await _verify_checkpoint_ownership(db, checkpoint_id, current_user)

    if row["resolved_at"] is not None:
        raise HTTPException(400, "Checkpoint already resolved")

    task_id = row["task_id"]
    now = time.time()

    if body.action == "retry":
        if task_id:
            task_row = await db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_id,))
            ctx = json.loads(task_row["context_json"]) if task_row and task_row["context_json"] else []
            if body.guidance:
                ctx.append({
                    "type": "checkpoint_guidance",
                    "content": body.guidance,
                })
            await db.execute_write(
                "UPDATE tasks SET status = ?, context_json = ?, error = NULL, "
                "retry_count = 0, output_text = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
                (TaskStatus.PENDING, json.dumps(ctx), now, task_id),
            )

        # Ingest human guidance as a diagnostic resolution
        if DIAGNOSTIC_RAG_ENABLED and body.guidance:
            try:
                await diagnostic_ingester.ingest_resolution(
                    error_text=row["summary"],
                    resolution_text=body.guidance,
                    error_context=f"Checkpoint for task {task_id}",
                    tags=["checkpoint", "human-resolved"],
                    gotcha=body.gotcha,
                )
            except Exception:
                pass  # Ingestion failure never blocks checkpoint resolution

    elif body.action == "skip":
        if task_id:
            await db.execute_write(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.CANCELLED, now, task_id),
            )
    elif body.action == "fail":
        if task_id:
            await db.execute_write(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.FAILED, now, task_id),
            )

    # Mark checkpoint resolved
    response_text = f"Action: {body.action}"
    if body.guidance:
        response_text += f" | Guidance: {body.guidance}"

    await db.execute_write(
        "UPDATE checkpoints SET response = ?, resolved_at = ? WHERE id = ?",
        (response_text, now, checkpoint_id),
    )

    updated = await db.fetchone("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
    return CheckpointOut(**_row_to_checkpoint(updated))
