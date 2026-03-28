#  Orchestration Engine - Task Routes
#
#  Task management: list, detail, update, retry, cancel.
#  All endpoints enforce ownership via the parent project.
#
#  Depends on: container.py, models/schemas.py, middleware/auth.py
#  Used by:    app.py

import json
import time

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.config import MAX_TASK_RETRIES, STALENESS_TIMEOUT
from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.models.enums import TaskSortField, TaskStatus
from backend.models.schemas import BulkTaskAction, ReviewAction, TaskOut, TaskUpdate, VerifyAction

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _verify_task_ownership(db: Database, task_id: str, user: dict):
    """Fetch a task and verify the user owns its parent project. Returns the task row."""
    row = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    if not row:
        raise HTTPException(404, f"Task {task_id} not found")
    project = await db.fetchone("SELECT owner_id FROM projects WHERE id = $1", (row["project_id"],))
    if not project or (user.get("role") != "admin" and project["owner_id"] != user["id"]):
        raise HTTPException(403, "You do not own this task's project")
    return row


async def _row_to_dict(
    row, db: Database,
    deps_list: list[str] | None = None,
    dep_details: list[dict] | None = None,
) -> dict:
    """Convert a DB row to a TaskOut-compatible dict.

    If deps_list is None, fetches dependencies from the DB (single-task endpoints).
    For batch use, pass pre-loaded deps_list and dep_details to avoid N+1 queries.
    """
    if deps_list is None:
        dep_rows = await db.fetchall(
            "SELECT d.depends_on, t.title, t.status "
            "FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
            "WHERE d.task_id = $1",
            (row["id"],),
        )
        deps_list = [d["depends_on"] for d in dep_rows]
        dep_details = [
            {"task_id": d["depends_on"], "title": d["title"], "status": d["status"]}
            for d in dep_rows
        ]

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "plan_id": row["plan_id"],
        "title": row["title"],
        "description": row["description"],
        "task_type": row["task_type"],
        "priority": row["priority"],
        "status": row["status"],
        "model_tier": row["model_tier"],
        "model_used": row["model_used"],
        "tools": json.loads(row["tools_json"]) if row["tools_json"] else [],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "cost_usd": row["cost_usd"],
        "output_text": row["output_text"],
        "output_artifacts": json.loads(row["output_artifacts_json"]) if row["output_artifacts_json"] else [],
        "wave": row["wave"],
        "phase": row["phase"],
        "verification_status": row["verification_status"],
        "verification_notes": row["verification_notes"],
        "requirement_ids": json.loads(row["requirement_ids_json"]) if row["requirement_ids_json"] else [],
        "context": json.loads(row["context_json"]) if row["context_json"] else [],
        "error": row["error"],
        "depends_on": deps_list,
        "dependency_details": dep_details or [],
        "rationale": row["rationale"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "git_branch": row["git_branch"],
        "git_commit_sha": row["git_commit_sha"],
    }


async def _rows_to_tasks(rows, db: Database) -> list[dict]:
    """Batch-convert rows to TaskOut dicts with a single dep query (avoids N+1)."""
    if not rows:
        return []

    task_ids = [r["id"] for r in rows]
    placeholders = ",".join([f"${i+1}" for i in range(len(task_ids))])
    dep_rows = await db.fetchall(
        f"SELECT d.task_id, d.depends_on, t.title AS dep_title, t.status AS dep_status "
        f"FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
        f"WHERE d.task_id IN ({placeholders})",
        task_ids,
    )

    # Group deps and dep details by task_id
    deps_map: dict[str, list[str]] = {tid: [] for tid in task_ids}
    details_map: dict[str, list[dict]] = {tid: [] for tid in task_ids}
    for d in dep_rows:
        deps_map[d["task_id"]].append(d["depends_on"])
        details_map[d["task_id"]].append({
            "task_id": d["depends_on"],
            "title": d["dep_title"],
            "status": d["dep_status"],
        })

    return [
        await _row_to_dict(r, db, deps_map.get(r["id"], []), details_map.get(r["id"], []))
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/bulk")
@inject
async def bulk_task_action(
    body: BulkTaskAction,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Perform an action on multiple tasks at once.

    Returns {succeeded: [...], failed: [{id, reason}]}.
    """
    results: dict = {"succeeded": [], "failed": []}

    for task_id in body.task_ids:
        try:
            row = await _verify_task_ownership(db, task_id, current_user)
        except HTTPException as e:
            results["failed"].append({"id": task_id, "reason": e.detail})
            continue

        if body.action == "retry":
            if row["status"] != TaskStatus.FAILED:
                results["failed"].append({"id": task_id, "reason": "Not in failed state"})
                continue
            if row["retry_count"] >= MAX_TASK_RETRIES:
                results["failed"].append({"id": task_id, "reason": "Max retries reached"})
                continue
            await db.execute_write(
                "UPDATE tasks SET status = $1, error = NULL, output_text = NULL, "
                "retry_count = retry_count + 1, updated_at = $2 WHERE id = $3",
                (TaskStatus.PENDING, time.time(), task_id),
            )
            results["succeeded"].append(task_id)

        elif body.action == "cancel":
            if row["status"] not in (TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.WAITING, TaskStatus.QUEUED):
                results["failed"].append({"id": task_id, "reason": f"Cannot cancel {row['status']} task"})
                continue
            await db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                (TaskStatus.CANCELLED, time.time(), task_id),
            )
            results["succeeded"].append(task_id)

    return results


@router.get("/project/{project_id}")
@inject
async def list_tasks(
    project_id: str,
    status: TaskStatus | None = None,
    wave: int | None = Query(default=None, ge=0),
    phase: str | None = Query(default=None, max_length=200),
    model_tier: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    sort: TaskSortField = TaskSortField.PRIORITY,
    sort_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    exclude_output: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[TaskOut]:
    """List all tasks for a project with optional filtering and sorting."""
    # Verify project ownership
    from backend.routes.projects import _get_owned_project
    await _get_owned_project(db, project_id, current_user)

    query = "SELECT * FROM tasks WHERE project_id = $1"
    params: list = [project_id]

    if status:
        params.append(status.value)
        query += f" AND status = ${len(params)}"
    if wave is not None:
        params.append(wave)
        query += f" AND wave = ${len(params)}"
    if phase:
        params.append(phase)
        query += f" AND phase = ${len(params)}"
    if model_tier:
        params.append(model_tier)
        query += f" AND model_tier = ${len(params)}"
    if search:
        params.append(search)
        query += f" AND (INSTR(LOWER(title), LOWER(${len(params)})) > 0"
        params.append(search)
        query += f" OR INSTR(LOWER(description), LOWER(${len(params)})) > 0)"

    # Sort column restricted to enum value (prevents injection)
    sort_column = sort.value
    direction = "ASC" if sort_dir == "asc" else "DESC"
    secondary = ", created_at ASC" if sort_column != "created_at" else ""
    query += f" ORDER BY {sort_column} {direction}{secondary}"
    params.append(limit)
    query += f" LIMIT ${len(params)}"
    params.append(offset)
    query += f" OFFSET ${len(params)}"

    rows = await db.fetchall(query, params)
    tasks = [TaskOut(**d) for d in await _rows_to_tasks(rows, db)]

    if exclude_output:
        for t in tasks:
            t.output_text = None
            t.output_artifacts = []

    return tasks


@router.get("/{task_id}")
@inject
async def get_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskOut:
    """Get task detail including output and cost."""
    row = await _verify_task_ownership(db, task_id, current_user)
    return TaskOut(**await _row_to_dict(row, db))


@router.patch("/{task_id}")
@inject
async def update_task(
    task_id: str,
    body: TaskUpdate,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskOut:
    """Edit task before execution (description, model tier, priority)."""
    row = await _verify_task_ownership(db, task_id, current_user)

    if row["status"] in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
        raise HTTPException(400, "Cannot edit a running or completed task")

    updates = []
    params = []
    if body.title is not None:
        params.append(body.title)
        updates.append(f"title = ${len(params)}")
    if body.description is not None:
        params.append(body.description)
        updates.append(f"description = ${len(params)}")
    if body.model_tier is not None:
        params.append(body.model_tier.value)
        updates.append(f"model_tier = ${len(params)}")
    if body.priority is not None:
        params.append(body.priority)
        updates.append(f"priority = ${len(params)}")
    if body.max_tokens is not None:
        params.append(body.max_tokens)
        updates.append(f"max_tokens = ${len(params)}")

    if not updates:
        raise HTTPException(400, "No fields to update")

    params.append(time.time())
    updates.append(f"updated_at = ${len(params)}")
    params.append(task_id)

    await db.execute_write(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ${len(params)}",
        params,
    )

    row = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    return TaskOut(**await _row_to_dict(row, db))


@router.post("/{task_id}/retry")
@inject
async def retry_task(
    task_id: str,
    force: bool = Query(False, description="Force-retry a running task (zombie recovery)"),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskOut:
    """Retry a failed or zombie running task.

    For running tasks, pass force=true. A staleness check prevents
    accidentally killing genuinely active tasks — the task must have
    been running longer than the staleness timeout.
    """
    row = await _verify_task_ownership(db, task_id, current_user)

    if row["status"] == TaskStatus.RUNNING:
        if not force:
            raise HTTPException(
                400,
                "Task is running. Pass force=true to retry a zombie task.",
            )
        # Guard against killing a genuinely active task
        started_at = row["started_at"] or 0
        if time.time() - started_at < STALENESS_TIMEOUT:
            raise HTTPException(
                409,
                f"Task has only been running {int(time.time() - started_at)}s "
                f"(staleness threshold: {int(STALENESS_TIMEOUT)}s). "
                "Wait for the timeout or cancel it first.",
            )
    elif row["status"] != TaskStatus.FAILED:
        raise HTTPException(400, "Can only retry failed or zombie running tasks")

    if row["retry_count"] >= MAX_TASK_RETRIES:
        raise HTTPException(400, f"Maximum retry limit reached ({MAX_TASK_RETRIES})")

    await db.execute_write(
        "UPDATE tasks SET status = $1, error = NULL, output_text = NULL, "
        "started_at = NULL, retry_count = retry_count + 1, updated_at = $2 WHERE id = $3",
        (TaskStatus.PENDING, time.time(), task_id),
    )

    row = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    return TaskOut(**await _row_to_dict(row, db))


@router.post("/{task_id}/cancel")
@inject
async def cancel_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskOut:
    """Cancel a pending or queued task."""
    row = await _verify_task_ownership(db, task_id, current_user)
    if row["status"] not in (TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.WAITING, TaskStatus.QUEUED):
        raise HTTPException(400, f"Cannot cancel task in '{row['status']}' state")

    await db.execute_write(
        "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
        (TaskStatus.CANCELLED, time.time(), task_id),
    )

    row = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    return TaskOut(**await _row_to_dict(row, db))


@router.post("/{task_id}/review")
@inject
async def review_task(
    task_id: str,
    body: ReviewAction,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> TaskOut:
    """Respond to a task in NEEDS_REVIEW status.

    Actions:
        approve — accept the output as-is, mark task COMPLETED.
        retry — reset to PENDING with user feedback appended to context.
    """
    row = await _verify_task_ownership(db, task_id, current_user)
    if row["status"] != TaskStatus.NEEDS_REVIEW:
        raise HTTPException(400, "Task is not in needs_review status")

    if body.action == "approve":
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            (TaskStatus.COMPLETED, time.time(), task_id),
        )
    elif body.action == "retry":
        if row["retry_count"] >= MAX_TASK_RETRIES:
            raise HTTPException(400, f"Maximum retry limit reached ({MAX_TASK_RETRIES})")
        ctx = json.loads(row["context_json"]) if row["context_json"] else []
        if body.feedback:
            ctx.append({
                "type": "review_feedback",
                "content": body.feedback,
            })
        await db.execute_write(
            "UPDATE tasks SET status = $1, context_json = $2, "
            "verification_status = NULL, verification_notes = NULL, "
            "output_text = NULL, completed_at = NULL, "
            "retry_count = retry_count + 1, updated_at = $3 WHERE id = $4",
            (TaskStatus.PENDING, json.dumps(ctx), time.time(), task_id),
        )

    updated = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    return TaskOut(**await _row_to_dict(updated, db))


@router.post("/{task_id}/verify")
@inject
async def verify_task(
    task_id: str,
    body: VerifyAction,
    db: Database = Depends(Provide[Container.db]),
) -> dict:
    """Submit a verification verdict for a completed task.

    Internal endpoint — called by the Mimir agent running on localhost.
    No auth required: the task ID is a UUID and marking a task verified
    is not a security-sensitive operation.
    """
    row = await db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
    if not row:
        raise HTTPException(404, f"Task {task_id} not found")
    if row["status"] not in (TaskStatus.COMPLETED, TaskStatus.RUNNING):
        raise HTTPException(400, f"Cannot verify task in '{row['status']}' state")

    retry_count = row.get("retry_count") or 0
    max_retries = row.get("max_retries") or MAX_TASK_RETRIES

    if body.verdict == "passed":
        await db.execute_write(
            "UPDATE tasks SET verification_status = $1, verification_notes = $2, updated_at = $3 WHERE id = $4",
            ("passed", body.feedback[:500] if body.feedback else None, time.time(), task_id),
        )
        return {"accepted": True, "message": "Task marked as verified."}

    elif body.verdict == "gaps_found":
        if retry_count >= max_retries:
            await db.execute_write(
                "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, updated_at = $4 WHERE id = $5",
                (TaskStatus.NEEDS_REVIEW, "gaps_found", body.feedback[:500], time.time(), task_id),
            )
            return {"accepted": True, "message": "Max retries reached. Task sent to human review."}

        ctx = json.loads(row["context_json"]) if row["context_json"] else []
        if body.feedback:
            ctx.append({"type": "verification_feedback", "content": body.feedback})
        await db.execute_write(
            "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, "
            "context_json = $4, retry_count = $5, output_text = NULL, completed_at = NULL, updated_at = $6 WHERE id = $7",
            (TaskStatus.PENDING, "gaps_found", body.feedback[:500], json.dumps(ctx), retry_count + 1, time.time(), task_id),
        )
        return {"accepted": True, "message": "Task reset for retry with verification feedback."}

    else:  # human_needed
        await db.execute_write(
            "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, updated_at = $4 WHERE id = $5",
            (TaskStatus.NEEDS_REVIEW, "human_needed", body.feedback[:500], time.time(), task_id),
        )
        return {"accepted": True, "message": "Task flagged for human review."}


@router.post("/{task_id}/expand", status_code=201)
@inject
async def expand_epic(
    task_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> dict:
    """Expand an L0 epic (task) into a new L2 project.

    Creates a new project using the epic's title as the name and its
    description as requirements, inheriting the parent project's
    repo_path and config. Returns the new project ID.
    """
    import uuid
    from backend.models.enums import ProjectStatus

    task_row = await _verify_task_ownership(db, task_id, current_user)

    # Get parent project for repo_path and config
    parent = await db.fetchone(
        "SELECT * FROM projects WHERE id = $1", (task_row["project_id"],)
    )
    if not parent:
        raise HTTPException(404, "Parent project not found")

    parent_config = json.loads(parent["config_json"]) if parent["config_json"] else {}

    # Build new project config — inherit review_cycle, upgrade rigor to L2
    new_config = dict(parent_config)
    new_config["planning_rigor"] = "L2"
    new_config["expanded_from"] = {
        "project_id": task_row["project_id"],
        "task_id": task_id,
        "epic_title": task_row["title"],
    }

    new_id = uuid.uuid4().hex[:12]
    now = time.time()

    await db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, config_json, "
        "owner_id, repo_path, git_base_branch, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        (
            new_id,
            task_row["title"],
            task_row["description"],
            ProjectStatus.DRAFT,
            json.dumps(new_config),
            current_user["id"],
            parent["repo_path"],
            parent["git_base_branch"],
            now, now,
        ),
    )

    return {"project_id": new_id, "name": task_row["title"]}
