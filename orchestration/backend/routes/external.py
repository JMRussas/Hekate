#  Orchestration Engine - External Execution Routes
#
#  REST endpoints for external task executors (e.g., Claude Code via MCP).
#  Supports claiming, submitting results, and releasing tasks.
#
#  Depends on: container.py, middleware/auth.py, services/task_lifecycle.py,
#              models/schemas.py, models/enums.py
#  Used by:    app.py

import json
import logging
import time

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.models.enums import (
    ExecutionMode,
    ModelTier,
    ProjectStatus,
    TaskStatus,
)
from backend.models.schemas import (
    TaskClaimResponse,
    TaskResultSubmission,
    TaskResultResponse,
)
from backend.services.budget import BudgetManager
from backend.services.progress import ProgressManager
from backend.services.task_lifecycle import (
    complete_task_external,
)

logger = logging.getLogger("orchestration.external")

router = APIRouter(prefix="/external", tags=["external"])


async def _get_owned_project(project_id: str, user: dict, db: Database) -> dict:
    """Fetch project, verify ownership."""
    row = await db.fetchone(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    if row["owner_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Project not found")
    return row


@router.get("/{project_id}/claimable")
@inject
async def list_claimable_tasks(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """List tasks that are claimable by an external executor.

    Returns tasks that are PENDING with all dependencies completed,
    filtered by the project's execution mode.
    """
    project = await _get_owned_project(project_id, user, db)

    if project["status"] != ProjectStatus.EXECUTING:
        return []

    config = json.loads(project["config_json"] or "{}")
    execution_mode = config.get("execution_mode", "auto")

    if execution_mode == ExecutionMode.AUTO:
        return []  # Auto mode — engine handles everything

    # Determine current wave
    _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED,
                 TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW)
    wave_row = await db.fetchone(
        "SELECT MIN(wave) as w FROM tasks "
        "WHERE project_id = ? AND status NOT IN (?, ?, ?, ?)",
        (project_id, *_TERMINAL),
    )
    current_wave = wave_row["w"] if wave_row and wave_row["w"] is not None else 0

    # Find claimable tasks: pending, all deps completed, current wave
    tasks = await db.fetchall(
        "SELECT t.id, t.title, t.description, t.model_tier, t.wave, t.priority, "
        "t.phase, t.task_type "
        "FROM tasks t "
        "LEFT JOIN task_deps d ON d.task_id = t.id "
        "LEFT JOIN tasks dep ON dep.id = d.depends_on AND dep.status != ? "
        "WHERE t.project_id = ? AND t.status = ? AND t.wave = ? "
        "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
        "ORDER BY t.priority ASC, t.created_at ASC",
        (TaskStatus.COMPLETED, project_id, TaskStatus.PENDING, current_wave),
    )

    result = []
    for t in tasks:
        tier = ModelTier(t["model_tier"])

        # Hybrid mode: only expose non-Ollama tasks
        if execution_mode == ExecutionMode.HYBRID and tier == ModelTier.OLLAMA:
            continue

        # Fetch dependency IDs for display
        deps = await db.fetchall(
            "SELECT depends_on FROM task_deps WHERE task_id = ?", (t["id"],)
        )

        result.append({
            "id": t["id"],
            "title": t["title"],
            "description": t["description"][:500],
            "model_tier": t["model_tier"],
            "wave": t["wave"],
            "priority": t["priority"],
            "phase": t["phase"],
            "task_type": t["task_type"],
            "depends_on": [d["depends_on"] for d in deps],
        })

    return result


@router.post("/tasks/{task_id}/claim")
@inject
async def claim_task(
    task_id: str,
    user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskClaimResponse:
    """Atomically claim a task for external execution.

    Returns full task details including context and tools.
    Returns 409 if the task is not claimable (already claimed or not pending).
    """
    # Verify the task exists and user owns the project
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await _get_owned_project(task["project_id"], user, db)

    if project["status"] != ProjectStatus.EXECUTING:
        raise HTTPException(
            status_code=409,
            detail=f"Project is not executing (status: {project['status']})",
        )

    # Check execution mode allows external claiming
    config = json.loads(project["config_json"] or "{}")
    execution_mode = config.get("execution_mode", "auto")
    if execution_mode == ExecutionMode.AUTO:
        raise HTTPException(
            status_code=409,
            detail="Project uses auto execution mode — external claiming not allowed",
        )

    tier = ModelTier(task["model_tier"])
    if execution_mode == ExecutionMode.HYBRID and tier == ModelTier.OLLAMA:
        raise HTTPException(
            status_code=409,
            detail="Ollama tasks are handled internally in hybrid mode",
        )

    # Atomic claim via CAS
    now = time.time()
    cursor = await db.execute_write(
        "UPDATE tasks SET status = ?, claimed_by = ?, claimed_at = ?, "
        "started_at = ?, updated_at = ? "
        "WHERE id = ? AND status = ?",
        (TaskStatus.RUNNING, user["id"], now, now, now,
         task_id, TaskStatus.PENDING),
    )
    if cursor.rowcount == 0:
        raise HTTPException(
            status_code=409,
            detail="Task not claimable — already claimed or not pending",
        )

    # Re-fetch with updated status
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    # Parse JSON fields
    context = json.loads(task["context_json"]) if task["context_json"] else []
    tools = json.loads(task["tools_json"]) if task["tools_json"] else []
    requirement_ids = json.loads(task["requirement_ids_json"]) if task["requirement_ids_json"] else []

    # Fetch dependency IDs
    deps = await db.fetchall(
        "SELECT depends_on FROM task_deps WHERE task_id = ?", (task_id,)
    )

    logger.info("Task %s claimed by user %s", task_id, user["id"])

    return TaskClaimResponse(
        id=task["id"],
        project_id=task["project_id"],
        title=task["title"],
        description=task["description"],
        task_type=task["task_type"],
        model_tier=task["model_tier"],
        wave=task["wave"],
        priority=task["priority"],
        phase=task["phase"],
        system_prompt=task["system_prompt"] or "",
        context=context,
        tools=tools,
        depends_on=[d["depends_on"] for d in deps],
        max_tokens=task["max_tokens"],
        requirement_ids=requirement_ids,
    )


@router.post("/tasks/{task_id}/result")
@inject
async def submit_task_result(
    task_id: str,
    body: TaskResultSubmission,
    user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
    budget: BudgetManager = Depends(Provide[Container.budget]),
    progress: ProgressManager = Depends(Provide[Container.progress]),
) -> TaskResultResponse:
    """Submit the result of an externally-executed task.

    Handles verification, context forwarding, and completion.
    """
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership and claim
    await _get_owned_project(task["project_id"], user, db)
    if task["status"] != TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Task is not running (status: {task['status']})",
        )
    if task["claimed_by"] != user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Task is claimed by a different user",
        )

    # Process the result
    result = await complete_task_external(
        task_id=task_id,
        task_row=task,
        project_id=task["project_id"],
        output_text=body.output_text,
        model_used=body.model_used,
        prompt_tokens=body.prompt_tokens,
        completion_tokens=body.completion_tokens,
        db=db,
        budget=budget,
        progress=progress,
    )

    # Find next claimable task for convenience
    next_task_id = None
    config = json.loads((await db.fetchone(
        "SELECT config_json FROM projects WHERE id = ?", (task["project_id"],)
    ))["config_json"] or "{}")
    execution_mode = config.get("execution_mode", "auto")
    if execution_mode != ExecutionMode.AUTO:
        _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED,
                     TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW)
        wave_row = await db.fetchone(
            "SELECT MIN(wave) as w FROM tasks "
            "WHERE project_id = ? AND status NOT IN (?, ?, ?, ?)",
            (task["project_id"], *_TERMINAL),
        )
        if wave_row and wave_row["w"] is not None:
            next_row = await db.fetchone(
                "SELECT t.id FROM tasks t "
                "LEFT JOIN task_deps d ON d.task_id = t.id "
                "LEFT JOIN tasks dep ON dep.id = d.depends_on AND dep.status != ? "
                "WHERE t.project_id = ? AND t.status = ? AND t.wave = ? "
                "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
                "ORDER BY t.priority ASC LIMIT 1",
                (TaskStatus.COMPLETED, task["project_id"],
                 TaskStatus.PENDING, wave_row["w"]),
            )
            if next_row:
                next_task_id = next_row["id"]

    return TaskResultResponse(
        task_id=task_id,
        status=result["status"],
        verification_status=result.get("verification_status"),
        verification_notes=result.get("verification_notes"),
        next_claimable_task_id=next_task_id,
    )


@router.post("/tasks/{task_id}/release")
@inject
async def release_task(
    task_id: str,
    user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Release a claimed task back to pending.

    Does not increment retry_count — release is intentional, not a failure.
    """
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    await _get_owned_project(task["project_id"], user, db)

    if task["status"] != TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Task is not running (status: {task['status']})",
        )
    if task["claimed_by"] != user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Task is claimed by a different user",
        )

    await db.execute_write(
        "UPDATE tasks SET status = ?, claimed_by = NULL, claimed_at = NULL, "
        "started_at = NULL, output_text = NULL, updated_at = ? "
        "WHERE id = ?",
        (TaskStatus.PENDING, time.time(), task_id),
    )

    logger.info("Task %s released by user %s", task_id, user["id"])
    return {"status": "released", "task_id": task_id}
