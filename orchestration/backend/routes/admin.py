#  Orchestration Engine - Admin Routes
#
#  Admin-only endpoints: user management, system stats.
#
#  Depends on: container.py, models/schemas.py, middleware/auth.py
#  Used by:    app.py

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import require_admin
from backend.models.schemas import AdminStats, AdminUserOut, AdminUserUpdate

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

@router.get("/users")
@inject
async def list_users(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> list[AdminUserOut]:
    """List all users with project counts."""
    rows = await db.fetchall(
        "SELECT id, email, display_name, role, is_active, created_at, last_login_at "
        "FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )

    # Batch project counts
    user_ids = [r["id"] for r in rows]
    if user_ids:
        placeholders = ",".join("?" * len(user_ids))
        counts = await db.fetchall(
            f"SELECT owner_id, COUNT(*) as cnt FROM projects "
            f"WHERE owner_id IN ({placeholders}) GROUP BY owner_id",
            user_ids,
        )
        count_map = {c["owner_id"]: c["cnt"] for c in counts}
    else:
        count_map = {}

    return [
        AdminUserOut(
            id=r["id"],
            email=r["email"],
            display_name=r["display_name"] or "",
            role=r["role"],
            is_active=bool(r["is_active"]),
            created_at=r["created_at"],
            last_login_at=r["last_login_at"],
            project_count=count_map.get(r["id"], 0),
        )
        for r in rows
    ]


@router.patch("/users/{user_id}")
@inject
async def update_user(
    user_id: str,
    body: AdminUserUpdate,
    admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> AdminUserOut:
    """Update a user's role or active status."""
    row = await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(404, "User not found")

    # Self-protection guards
    if user_id == admin["id"]:
        if body.is_active is False:
            raise HTTPException(400, "Cannot deactivate your own account")
        if body.role and body.role != "admin":
            raise HTTPException(400, "Cannot change your own role")

    # Last-admin protection: prevent removing the final active admin
    is_demoting_admin = (body.role is not None and body.role != "admin" and row["role"] == "admin")
    is_deactivating_admin = (body.is_active is False and row["role"] == "admin")
    if is_demoting_admin or is_deactivating_admin:
        count = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM users WHERE role = 'admin' AND is_active = 1"
        )
        if count["cnt"] <= 1:
            raise HTTPException(400, "Cannot remove the last active admin")

    updates = []
    params = []
    if body.role is not None:
        updates.append("role = ?")
        params.append(body.role)
    if body.is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if body.is_active else 0)

    if not updates:
        raise HTTPException(400, "No fields to update")

    params.append(user_id)
    await db.execute_write(
        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
        params,
    )

    updated = await db.fetchone(
        "SELECT id, email, display_name, role, is_active, created_at, last_login_at "
        "FROM users WHERE id = ?", (user_id,)
    )
    count = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM projects WHERE owner_id = ?", (user_id,)
    )
    return AdminUserOut(
        id=updated["id"],
        email=updated["email"],
        display_name=updated["display_name"] or "",
        role=updated["role"],
        is_active=bool(updated["is_active"]),
        created_at=updated["created_at"],
        last_login_at=updated["last_login_at"],
        project_count=count["cnt"] if count else 0,
    )


# ---------------------------------------------------------------------------
# System Stats
# ---------------------------------------------------------------------------

@router.get("/stats")
@inject
async def get_stats(
    _admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> AdminStats:
    """Aggregated system statistics."""
    # Users
    user_row = await db.fetchone("SELECT COUNT(*) as total FROM users")
    active_row = await db.fetchone("SELECT COUNT(*) as cnt FROM users WHERE is_active = 1")

    # Projects by status
    proj_rows = await db.fetchall(
        "SELECT status, COUNT(*) as cnt FROM projects GROUP BY status"
    )
    projects_by_status = {r["status"]: r["cnt"] for r in proj_rows}
    total_projects = sum(projects_by_status.values())

    # Tasks by status
    task_rows = await db.fetchall(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
    )
    tasks_by_status = {r["status"]: r["cnt"] for r in task_rows}
    total_tasks = sum(tasks_by_status.values())

    # Spending by model
    spend_rows = await db.fetchall(
        "SELECT model, SUM(cost_usd) as total FROM usage_log GROUP BY model"
    )
    spend_by_model = {r["model"]: round(r["total"], 6) for r in spend_rows}
    total_spend = sum(spend_by_model.values())

    # Completion rate
    completed = tasks_by_status.get("completed", 0)
    failed = tasks_by_status.get("failed", 0)
    rate = completed / (completed + failed) if (completed + failed) > 0 else 0.0

    return AdminStats(
        total_users=user_row["total"],
        active_users=active_row["cnt"],
        total_projects=total_projects,
        projects_by_status=projects_by_status,
        total_tasks=total_tasks,
        tasks_by_status=tasks_by_status,
        total_spend_usd=round(total_spend, 6),
        spend_by_model=spend_by_model,
        task_completion_rate=round(rate, 4),
    )
