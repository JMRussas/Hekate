#  Orchestration Engine - Project Routes
#
#  CRUD for orchestration projects + plan/execute triggers.
#  All endpoints enforce ownership: users see/modify only their own projects.
#  Admins can access all projects.
#
#  Depends on: container.py, models/schemas.py, services/planner.py, services/decomposer.py, middleware/auth.py
#  Used by:    app.py

import json
import time
import uuid

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.container import Container
from backend.db.connection import Database
from backend.exceptions import (
    CycleDetectedError,
    NotFoundError,
    OrchestrationError,
)
from backend.rate_limit import limiter
from backend.middleware.auth import get_current_user
from backend.models.enums import PlanStatus, ProjectStatus, TaskStatus
from backend.models.schemas import FindingOut, PlanOut, ProjectCreate, ProjectOut, ProjectUpdate
from backend.services.decomposer import DecomposerService
from backend.services.planner import PlannerService

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _row_to_project(
    row, db: Database,
    include_task_summary: bool = False,
    preloaded_summary: dict | None = None,
) -> dict:
    """Convert a DB row to a ProjectOut-compatible dict.

    For batch use, pass preloaded_summary to avoid per-project DB queries.
    """
    config = json.loads(row["config_json"]) if row["config_json"] else {}
    git_state = json.loads(row["git_state_json"]) if row["git_state_json"] else {}
    data = {
        "id": row["id"],
        "name": row["name"],
        "requirements": row["requirements"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "config": config,
        "planning_rigor": config.get("planning_rigor", "L2"),
        "repo_path": row["repo_path"],
        "git_base_branch": row["git_base_branch"],
        "git_project_branch": row["git_project_branch"],
        "git_state": git_state,
    }

    if include_task_summary:
        if preloaded_summary is not None:
            data["task_summary"] = preloaded_summary
        else:
            tasks = await db.fetchall(
                "SELECT status, COUNT(*) as cnt FROM tasks WHERE project_id = ? GROUP BY status",
                (row["id"],),
            )
            summary = {"total": 0, "completed": 0, "running": 0, "failed": 0}
            for t in tasks:
                summary["total"] += t["cnt"]
                if t["status"] in summary:
                    summary[t["status"]] = t["cnt"]
            data["task_summary"] = summary

    return data


async def _get_owned_project(db: Database, project_id: str, user: dict):
    """Fetch a project and verify ownership. Raises 404/403."""
    row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(404, f"Project {project_id} not found")
    # Admins can access all; NULL owner_id is admin-only (no owner = no access for regular users)
    if user.get("role") != "admin" and row["owner_id"] != user["id"]:
        raise HTTPException(403, "You do not own this project")
    return row


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
@inject
async def create_project(
    body: ProjectCreate,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> ProjectOut:
    project_id = uuid.uuid4().hex[:12]
    now = time.time()

    config = dict(body.config)
    config["planning_rigor"] = body.planning_rigor.value

    await db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, config_json, owner_id, "
        "repo_path, git_base_branch, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, body.name, body.requirements, ProjectStatus.DRAFT,
         json.dumps(config), current_user["id"],
         body.repo_path, body.git_base_branch, now, now),
    )

    row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    return ProjectOut(**await _row_to_project(row, db))


@router.get("")
@inject
async def list_projects(
    status: ProjectStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[ProjectOut]:
    if current_user.get("role") == "admin":
        query = "SELECT * FROM projects WHERE 1=1"
        params: list = []
    else:
        query = "SELECT * FROM projects WHERE owner_id = ?"
        params = [current_user["id"]]

    if status:
        query += " AND status = ?"
        params.append(status.value)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = await db.fetchall(query, params)
    if not rows:
        return []

    # Batch-load task summaries in a single query (avoids N+1)
    project_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(project_ids))
    summary_rows = await db.fetchall(
        f"SELECT project_id, status, COUNT(*) as cnt FROM tasks "
        f"WHERE project_id IN ({placeholders}) GROUP BY project_id, status",
        project_ids,
    )
    summaries: dict[str, dict] = {}
    for sr in summary_rows:
        pid = sr["project_id"]
        if pid not in summaries:
            summaries[pid] = {"total": 0, "completed": 0, "running": 0, "failed": 0}
        summaries[pid]["total"] += sr["cnt"]
        if sr["status"] in summaries[pid]:
            summaries[pid][sr["status"]] = sr["cnt"]

    return [
        ProjectOut(**await _row_to_project(
            r, db, include_task_summary=True,
            preloaded_summary=summaries.get(r["id"], {"total": 0, "completed": 0, "running": 0, "failed": 0}),
        ))
        for r in rows
    ]


@router.get("/{project_id}")
@inject
async def get_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> ProjectOut:
    row = await _get_owned_project(db, project_id, current_user)
    return ProjectOut(**await _row_to_project(row, db, include_task_summary=True))


@router.patch("/{project_id}")
@inject
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> ProjectOut:
    row = await _get_owned_project(db, project_id, current_user)

    updates = []
    params = []
    if body.name is not None:
        updates.append("name = ?")
        params.append(body.name)
    if body.requirements is not None:
        updates.append("requirements = ?")
        params.append(body.requirements)
    if body.config is not None or body.planning_rigor is not None:
        # Start from body.config if provided, else existing config
        if body.config is not None:
            merged_config = dict(body.config)
        else:
            merged_config = json.loads(row["config_json"]) if row["config_json"] else {}
        # Overlay planning_rigor if explicitly set
        if body.planning_rigor is not None:
            merged_config["planning_rigor"] = body.planning_rigor.value
        updates.append("config_json = ?")
        params.append(json.dumps(merged_config))
    if body.repo_path is not None:
        updates.append("repo_path = ?")
        params.append(body.repo_path)
    if body.git_base_branch is not None:
        updates.append("git_base_branch = ?")
        params.append(body.git_base_branch)

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append("updated_at = ?")
    params.append(time.time())
    params.append(project_id)

    await db.execute_write(
        f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
        params,
    )

    row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    return ProjectOut(**await _row_to_project(row, db, include_task_summary=True))


@router.delete("/{project_id}", status_code=204)
@inject
async def delete_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    await _get_owned_project(db, project_id, current_user)
    # Cascade deletes handle plans, tasks, deps, events
    await db.execute_write("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@router.post("/{project_id}/plan")
@limiter.limit("5/minute")
@inject
async def trigger_plan(
    request: Request,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
    planner: PlannerService = Depends(Provide[Container.planner]),
):
    """Generate a plan from the project's requirements using Claude."""
    row = await _get_owned_project(db, project_id, current_user)

    # Reject planning for projects in terminal or active states
    if row["status"] in (ProjectStatus.EXECUTING, ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
        raise HTTPException(400, f"Cannot plan a project in '{row['status']}' state")

    result = await planner.generate(project_id)
    return result


@router.get("/{project_id}/plans")
@inject
async def list_plans(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[PlanOut]:
    """List plan versions for a project."""
    await _get_owned_project(db, project_id, current_user)

    rows = await db.fetchall(
        "SELECT * FROM plans WHERE project_id = ? ORDER BY version DESC LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    )
    return [
        PlanOut(
            id=r["id"],
            project_id=r["project_id"],
            version=r["version"],
            model_used=r["model_used"],
            prompt_tokens=r["prompt_tokens"],
            completion_tokens=r["completion_tokens"],
            cost_usd=r["cost_usd"],
            plan=json.loads(r["plan_json"]),
            status=r["status"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/{project_id}/plans/{plan_id}/approve")
@inject
async def approve_plan(
    project_id: str,
    plan_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
    decomposer: DecomposerService = Depends(Provide[Container.decomposer]),
):
    """Approve a plan and decompose it into executable tasks."""
    await _get_owned_project(db, project_id, current_user)

    row = await db.fetchone("SELECT * FROM plans WHERE id = ? AND project_id = ?", (plan_id, project_id))
    if not row:
        raise HTTPException(404, f"Plan {plan_id} not found")
    if row["status"] != PlanStatus.DRAFT:
        raise HTTPException(400, f"Plan is already {row['status']}")

    try:
        result = await decomposer.decompose(project_id, plan_id)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except CycleDetectedError as e:
        raise HTTPException(422, str(e))
    except OrchestrationError as e:
        raise HTTPException(400, str(e))

    return result


@router.post("/{project_id}/execute")
@inject
async def start_execution(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Start executing approved tasks for a project."""
    row = await _get_owned_project(db, project_id, current_user)
    if row["status"] not in (ProjectStatus.READY, ProjectStatus.PAUSED):
        raise HTTPException(400, f"Project must be in 'ready' or 'paused' state, got '{row['status']}'")

    await db.execute_write(
        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
        (ProjectStatus.EXECUTING, time.time(), project_id),
    )

    # Executor will pick up tasks on its next tick
    return {"status": "executing", "project_id": project_id}


@router.post("/{project_id}/pause")
@inject
async def pause_execution(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Pause execution — no new tasks will start."""
    row = await _get_owned_project(db, project_id, current_user)
    if row["status"] != ProjectStatus.EXECUTING:
        raise HTTPException(400, "Project is not executing")

    await db.execute_write(
        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
        (ProjectStatus.PAUSED, time.time(), project_id),
    )
    return {"status": "paused", "project_id": project_id}


@router.post("/{project_id}/cancel")
@inject
async def cancel_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Cancel project — cancel all pending tasks."""
    await _get_owned_project(db, project_id, current_user)

    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = ?, updated_at = ? "
        "WHERE project_id = ? AND status IN (?, ?, ?, ?)",
        (TaskStatus.CANCELLED, now, project_id,
         TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.QUEUED, TaskStatus.RUNNING),
    )
    await db.execute_write(
        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
        (ProjectStatus.CANCELLED, now, project_id),
    )
    return {"status": "cancelled", "project_id": project_id}


@router.post("/{project_id}/clone", status_code=201)
@inject
async def clone_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> ProjectOut:
    """Clone a project: copies metadata, latest plan, and all tasks (reset to PENDING)."""
    row = await _get_owned_project(db, project_id, current_user)

    new_project_id = uuid.uuid4().hex[:12]
    now = time.time()

    async with db.transaction():
        # 1. Clone project row
        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_project_id, f"{row['name']} (clone)", row["requirements"],
             ProjectStatus.DRAFT, row["config_json"], current_user["id"], now, now),
        )

        # 2. Find latest approved plan (or latest draft)
        plan_row = await db.fetchone(
            "SELECT * FROM plans WHERE project_id = ? AND status = 'approved' ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        if not plan_row:
            plan_row = await db.fetchone(
                "SELECT * FROM plans WHERE project_id = ? ORDER BY version DESC LIMIT 1",
                (project_id,),
            )

        new_plan_id = None
        if plan_row:
            new_plan_id = uuid.uuid4().hex[:12]
            await db.execute_write(
                "INSERT INTO plans (id, project_id, version, model_used, prompt_tokens, "
                "completion_tokens, cost_usd, plan_json, status, created_at) "
                "VALUES (?, ?, 1, ?, 0, 0, 0.0, ?, 'draft', ?)",
                (new_plan_id, new_project_id, plan_row["model_used"], plan_row["plan_json"], now),
            )

        # 3. Clone tasks (reset status, clear output/cost/retry)
        old_tasks = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        )

        old_to_new: dict[str, str] = {}
        for old_task in old_tasks:
            new_task_id = uuid.uuid4().hex[:12]
            old_to_new[old_task["id"]] = new_task_id

            await db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
                "priority, status, model_tier, context_json, tools_json, system_prompt, "
                "max_tokens, wave, phase, requirement_ids_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_task_id, new_project_id, new_plan_id or old_task["plan_id"],
                 old_task["title"], old_task["description"], old_task["task_type"],
                 old_task["priority"], TaskStatus.PENDING, old_task["model_tier"],
                 old_task["context_json"], old_task["tools_json"], old_task["system_prompt"],
                 old_task["max_tokens"], old_task["wave"], old_task["phase"],
                 old_task["requirement_ids_json"], now, now),
            )

        # 4. Remap task dependencies
        if old_to_new:
            old_deps = await db.fetchall(
                "SELECT task_id, depends_on FROM task_deps WHERE task_id IN ({})".format(
                    ",".join("?" * len(old_to_new))
                ),
                list(old_to_new.keys()),
            )
            for dep in old_deps:
                new_from = old_to_new.get(dep["task_id"])
                new_to = old_to_new.get(dep["depends_on"])
                if new_from and new_to:
                    await db.execute_write(
                        "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
                        (new_from, new_to),
                    )

    new_row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (new_project_id,))
    return ProjectOut(**await _row_to_project(new_row, db, include_task_summary=True))


@router.get("/{project_id}/export")
@limiter.limit("5/minute")
@inject
async def export_project(
    request: Request,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Export full project data as downloadable JSON."""
    from fastapi.responses import JSONResponse
    from backend.routes.tasks import _rows_to_tasks

    row = await _get_owned_project(db, project_id, current_user)
    project_data = await _row_to_project(row, db, include_task_summary=True)

    # Plans
    plan_rows = await db.fetchall(
        "SELECT * FROM plans WHERE project_id = ? ORDER BY version DESC", (project_id,)
    )
    plans = [
        {
            "id": p["id"], "version": p["version"], "model_used": p["model_used"],
            "prompt_tokens": p["prompt_tokens"], "completion_tokens": p["completion_tokens"],
            "cost_usd": p["cost_usd"], "plan": json.loads(p["plan_json"]),
            "status": p["status"], "created_at": p["created_at"],
        }
        for p in plan_rows
    ]

    # Tasks
    task_rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? ORDER BY wave ASC, priority ASC", (project_id,)
    )
    tasks = await _rows_to_tasks(task_rows, db)

    # Events
    event_rows = await db.fetchall(
        "SELECT * FROM task_events WHERE project_id = ? ORDER BY timestamp ASC", (project_id,)
    )
    events = [
        {
            "id": e["id"], "task_id": e["task_id"], "event_type": e["event_type"],
            "message": e["message"],
            "data": json.loads(e["data_json"]) if e["data_json"] else None,
            "timestamp": e["timestamp"],
        }
        for e in event_rows
    ]

    # Checkpoints
    cp_rows = await db.fetchall(
        "SELECT * FROM checkpoints WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
    )
    checkpoints = [
        {
            "id": c["id"], "task_id": c["task_id"], "checkpoint_type": c["checkpoint_type"],
            "summary": c["summary"],
            "attempts": json.loads(c["attempts_json"]) if c["attempts_json"] else [],
            "question": c["question"], "response": c["response"],
            "resolved_at": c["resolved_at"], "created_at": c["created_at"],
        }
        for c in cp_rows
    ]

    # Usage
    usage_rows = await db.fetchall(
        "SELECT * FROM usage_log WHERE project_id = ? ORDER BY timestamp ASC", (project_id,)
    )
    usage = [
        {
            "id": u["id"], "task_id": u["task_id"], "provider": u["provider"],
            "model": u["model"], "prompt_tokens": u["prompt_tokens"],
            "completion_tokens": u["completion_tokens"], "cost_usd": u["cost_usd"],
            "purpose": u["purpose"], "timestamp": u["timestamp"],
        }
        for u in usage_rows
    ]

    # Knowledge
    knowledge_rows = await db.fetchall(
        "SELECT * FROM project_knowledge WHERE project_id = ? ORDER BY created_at ASC",
        (project_id,),
    )
    knowledge = [
        {
            "id": k["id"], "task_id": k["task_id"], "category": k["category"],
            "content": k["content"], "source_task_title": k["source_task_title"],
            "created_at": k["created_at"],
        }
        for k in knowledge_rows
    ]

    return JSONResponse(
        content={
            "exported_at": time.time(),
            "project": project_data,
            "plans": plans,
            "tasks": tasks,
            "events": events,
            "checkpoints": checkpoints,
            "usage": usage,
            "knowledge": knowledge,
        },
        headers={"Content-Disposition": f'attachment; filename="project_{project_id}.json"'},
    )


@router.get("/{project_id}/coverage")
@inject
async def get_coverage(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Show which requirements are covered by tasks.

    Parses project requirements into numbered items [R1], [R2], etc.
    and checks which are mapped to at least one task.
    """
    row = await _get_owned_project(db, project_id, current_user)
    requirements = row["requirements"] or ""

    # Parse requirement blocks (same numbering as planner.py)
    from backend.utils.json_utils import parse_requirements
    req_blocks = parse_requirements(requirements)
    all_req_ids = [f"R{i + 1}" for i in range(len(req_blocks))]

    # Gather requirement IDs from all tasks in this project
    task_rows = await db.fetchall(
        "SELECT requirement_ids_json FROM tasks WHERE project_id = ?",
        (project_id,),
    )
    covered: set[str] = set()
    for tr in task_rows:
        ids = json.loads(tr["requirement_ids_json"]) if tr["requirement_ids_json"] else []
        covered.update(ids)

    requirements_detail = []
    for i, req_id in enumerate(all_req_ids):
        requirements_detail.append({
            "id": req_id,
            "text": req_blocks[i],
            "covered": req_id in covered,
        })

    return {
        "project_id": project_id,
        "total_requirements": len(all_req_ids),
        "covered_count": sum(1 for r in requirements_detail if r["covered"]),
        "uncovered_count": sum(1 for r in requirements_detail if not r["covered"]),
        "requirements": requirements_detail,
    }


# ---------------------------------------------------------------------------
# Project Knowledge
# ---------------------------------------------------------------------------

@router.get("/{project_id}/knowledge")
@inject
async def list_knowledge(
    project_id: str,
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[FindingOut]:
    """List knowledge findings for a project, optionally filtered by category."""
    await _get_owned_project(db, project_id, current_user)

    if category:
        rows = await db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ? AND category = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (project_id, category, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )

    return [
        FindingOut(
            id=r["id"],
            project_id=r["project_id"],
            task_id=r["task_id"],
            category=r["category"],
            content=r["content"],
            source_task_title=r["source_task_title"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.delete("/{project_id}/knowledge/{finding_id}", status_code=204)
@inject
async def delete_finding(
    project_id: str,
    finding_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Delete a knowledge finding."""
    await _get_owned_project(db, project_id, current_user)

    row = await db.fetchone(
        "SELECT id FROM project_knowledge WHERE id = ? AND project_id = ?",
        (finding_id, project_id),
    )
    if not row:
        raise HTTPException(404, f"Finding {finding_id} not found")

    await db.execute_write(
        "DELETE FROM project_knowledge WHERE id = ?", (finding_id,),
    )
