#  Orchestration Engine - Usage Routes
#
#  Token usage, cost dashboard, and budget management endpoints.
#  Project-scoped queries enforce ownership. Global budget is admin-only.
#
#  Depends on: container.py, models/schemas.py, middleware/auth.py
#  Used by:    app.py

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user, require_admin
from backend.models.schemas import BudgetStatus, UsageSummary
from backend.services.budget import BudgetManager

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/summary")
@inject
async def get_usage_summary(
    project_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    budget: BudgetManager = Depends(Provide[Container.budget]),
    db: Database = Depends(Provide[Container.db]),
) -> UsageSummary:
    """Aggregate usage: total tokens, cost, by model/provider."""
    if project_id:
        # Scoped to a project — verify ownership
        from backend.routes.projects import _get_owned_project
        await _get_owned_project(db, project_id, current_user)
    elif current_user.get("role") != "admin":
        # Unscoped summary is admin-only (leaks system-wide data otherwise)
        raise HTTPException(
            status_code=403, detail="Admin access required for system-wide usage"
        )
    return await budget.get_usage_summary(project_id)


@router.get("/budget")
@inject
async def get_budget(
    _admin: dict = Depends(require_admin),
    budget: BudgetManager = Depends(Provide[Container.budget]),
) -> BudgetStatus:
    """Current budget status (daily/monthly limits vs spent). Admin only."""
    return await budget.get_budget_status()


@router.get("/daily")
@inject
async def get_daily_usage(
    days: int = Query(default=30, le=90),
    _admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> list[dict]:
    """Daily cost breakdown for the last N days. Admin only."""
    rows = await db.fetchall(
        "SELECT period_key, total_cost_usd, total_prompt_tokens, "
        "total_completion_tokens, api_call_count "
        "FROM budget_periods WHERE period_type = 'daily' "
        "ORDER BY period_key DESC LIMIT ?",
        (days,),
    )
    return [
        {
            "date": r["period_key"],
            "cost_usd": round(r["total_cost_usd"], 4),
            "prompt_tokens": r["total_prompt_tokens"],
            "completion_tokens": r["total_completion_tokens"],
            "api_calls": r["api_call_count"],
        }
        for r in reversed(rows)
    ]


@router.get("/by-project")
@inject
async def get_usage_by_project(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[dict]:
    """Cost breakdown per project. Filtered to owned projects (admins see all)."""
    if current_user.get("role") == "admin":
        rows = await db.fetchall(
            "SELECT u.project_id, p.name, SUM(u.cost_usd) as cost, "
            "SUM(u.prompt_tokens) as pt, SUM(u.completion_tokens) as ct, "
            "COUNT(*) as calls "
            "FROM usage_log u LEFT JOIN projects p ON p.id = u.project_id "
            "WHERE u.project_id IS NOT NULL "
            "GROUP BY u.project_id ORDER BY cost DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT u.project_id, p.name, SUM(u.cost_usd) as cost, "
            "SUM(u.prompt_tokens) as pt, SUM(u.completion_tokens) as ct, "
            "COUNT(*) as calls "
            "FROM usage_log u LEFT JOIN projects p ON p.id = u.project_id "
            "WHERE u.project_id IS NOT NULL AND p.owner_id = ? "
            "GROUP BY u.project_id ORDER BY cost DESC LIMIT ? OFFSET ?",
            (current_user["id"], limit, offset),
        )
    return [
        {
            "project_id": r["project_id"],
            "project_name": r["name"] or "Unknown",
            "cost_usd": round(r["cost"], 4),
            "prompt_tokens": r["pt"],
            "completion_tokens": r["ct"],
            "api_calls": r["calls"],
        }
        for r in rows
    ]
