#  Orchestration Engine - Analytics Routes
#
#  Admin-only analytics: cost breakdown, task outcomes, efficiency metrics.
#  Read-only queries on existing tables (no migrations needed).
#
#  All cost data sourced from usage_log (canonical per-API-call records).
#  Task counts sourced from tasks table (terminal statuses only).
#
#  Depends on: container.py, models/schemas.py, middleware/auth.py
#  Used by:    app.py

import time

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, Query

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import require_admin
from backend.models.enums import TaskStatus, VerificationResult
from backend.models.schemas import (
    AnalyticsCostBreakdown,
    AnalyticsEfficiency,
    AnalyticsTaskOutcomes,
    CostByModelTier,
    CostByProject,
    CostEfficiencyItem,
    DailyCostTrend,
    RetryByTier,
    TaskOutcomeByTier,
    VerificationByTier,
    WaveThroughput,
)

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])

_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.NEEDS_REVIEW.value,
)
_TERMINAL_PLACEHOLDERS = ",".join("?" * len(_TERMINAL_STATUSES))


# ---------------------------------------------------------------------------
# Cost Breakdown
# ---------------------------------------------------------------------------

@router.get("/cost-breakdown")
@inject
async def cost_breakdown(
    _admin: dict = Depends(require_admin),
    days: int = Query(default=30, ge=1, le=90),
    db: Database = Depends(Provide[Container.db]),
) -> AnalyticsCostBreakdown:
    """Cost breakdown by project, model tier, and daily trend.

    All cost data sourced from usage_log.  The `days` parameter filters
    all three sections to the requested time window.
    """
    cutoff = time.time() - (days * 86400)

    # By project — from usage_log joined with tasks (terminal only) and projects
    # Same terminal-status filter as by_model_tier so totals agree.
    project_rows = await db.fetchall(
        "SELECT u.project_id, p.name as project_name, "
        "SUM(u.cost_usd) as cost, COUNT(DISTINCT u.task_id) as task_count "
        "FROM usage_log u "
        "JOIN tasks t ON t.id = u.task_id "
        "LEFT JOIN projects p ON p.id = u.project_id "
        f"WHERE t.status IN ({_TERMINAL_PLACEHOLDERS}) "
        "AND u.project_id IS NOT NULL AND u.timestamp >= ? "
        "GROUP BY u.project_id ORDER BY cost DESC",
        (*_TERMINAL_STATUSES, cutoff),
    )
    by_project = [
        CostByProject(
            project_id=r["project_id"],
            project_name=r["project_name"] or "(deleted)",
            cost_usd=round(r["cost"] or 0, 6),
            task_count=r["task_count"],
        )
        for r in project_rows
    ]

    # By model tier — from usage_log joined with tasks for model_tier
    tier_rows = await db.fetchall(
        "SELECT t.model_tier, SUM(u.cost_usd) as cost, "
        "COUNT(DISTINCT u.task_id) as task_count "
        "FROM usage_log u "
        "JOIN tasks t ON t.id = u.task_id "
        f"WHERE t.status IN ({_TERMINAL_PLACEHOLDERS}) AND u.timestamp >= ? "
        "GROUP BY t.model_tier",
        (*_TERMINAL_STATUSES, cutoff),
    )
    by_model_tier = [
        CostByModelTier(
            model_tier=r["model_tier"],
            cost_usd=round(r["cost"] or 0, 6),
            task_count=r["task_count"],
            avg_cost_per_task=round((r["cost"] or 0) / r["task_count"], 6) if r["task_count"] else 0,
        )
        for r in tier_rows
    ]

    # Daily trend — from usage_log with same JOIN/filter as by_project/by_model_tier
    trend_rows = await db.fetchall(
        "SELECT DATE(u.timestamp, 'unixepoch') as date, "
        "SUM(u.cost_usd) as cost, COUNT(*) as api_calls "
        "FROM usage_log u "
        "JOIN tasks t ON t.id = u.task_id "
        f"WHERE t.status IN ({_TERMINAL_PLACEHOLDERS}) AND u.timestamp >= ? "
        "GROUP BY date ORDER BY date ASC",
        (*_TERMINAL_STATUSES, cutoff),
    )
    daily_trend = [
        DailyCostTrend(
            date=r["date"],
            cost_usd=round(r["cost"] or 0, 6),
            api_calls=r["api_calls"],
        )
        for r in trend_rows
    ]

    total = sum(t.cost_usd for t in by_model_tier)

    return AnalyticsCostBreakdown(
        by_project=by_project,
        by_model_tier=by_model_tier,
        daily_trend=daily_trend,
        total_cost_usd=round(total, 6),
    )


# ---------------------------------------------------------------------------
# Task Outcomes
# ---------------------------------------------------------------------------

@router.get("/task-outcomes")
@inject
async def task_outcomes(
    _admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> AnalyticsTaskOutcomes:
    """Task success rates and verification signal by model tier."""

    # Task outcomes by tier + status
    outcome_rows = await db.fetchall(
        "SELECT model_tier, status, COUNT(*) as cnt "
        f"FROM tasks WHERE status IN ({_TERMINAL_PLACEHOLDERS}) "
        "GROUP BY model_tier, status",
        _TERMINAL_STATUSES,
    )

    # Pivot into per-tier records
    tier_map: dict[str, dict] = {}
    for r in outcome_rows:
        tier = r["model_tier"]
        if tier not in tier_map:
            tier_map[tier] = {"completed": 0, "failed": 0, "cancelled": 0, "needs_review": 0}
        tier_map[tier][r["status"]] = r["cnt"]

    by_tier = []
    for tier, counts in sorted(tier_map.items()):
        total = counts["completed"] + counts["failed"] + counts["cancelled"] + counts["needs_review"]
        by_tier.append(TaskOutcomeByTier(
            model_tier=tier,
            total=total,
            completed=counts["completed"],
            failed=counts["failed"],
            needs_review=counts["needs_review"],
            success_rate=round(counts["completed"] / total, 4) if total else 0,
        ))

    # Verification by tier — terminal statuses only
    verif_rows = await db.fetchall(
        "SELECT model_tier, verification_status, COUNT(*) as cnt "
        "FROM tasks WHERE verification_status IS NOT NULL "
        f"AND status IN ({_TERMINAL_PLACEHOLDERS}) "
        "GROUP BY model_tier, verification_status",
        _TERMINAL_STATUSES,
    )

    verif_map: dict[str, dict] = {}
    for r in verif_rows:
        tier = r["model_tier"]
        if tier not in verif_map:
            verif_map[tier] = {"passed": 0, "gaps_found": 0, "human_needed": 0}
        vs = r["verification_status"]
        if vs in verif_map[tier]:
            verif_map[tier][vs] = r["cnt"]

    verification_by_tier = []
    for tier, counts in sorted(verif_map.items()):
        total = counts["passed"] + counts["gaps_found"] + counts["human_needed"]
        verification_by_tier.append(VerificationByTier(
            model_tier=tier,
            total_verified=total,
            passed=counts["passed"],
            gaps_found=counts["gaps_found"],
            human_needed=counts["human_needed"],
            pass_rate=round(counts["passed"] / total, 4) if total else 0,
        ))

    return AnalyticsTaskOutcomes(
        by_tier=by_tier,
        verification_by_tier=verification_by_tier,
    )


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------

@router.get("/efficiency")
@inject
async def efficiency(
    _admin: dict = Depends(require_admin),
    db: Database = Depends(Provide[Container.db]),
) -> AnalyticsEfficiency:
    """Retry rates, checkpoint counts, wave throughput, cost efficiency."""

    # Retries by tier — terminal statuses only
    retry_rows = await db.fetchall(
        "SELECT model_tier, COUNT(*) as total, "
        "SUM(CASE WHEN retry_count > 0 THEN 1 ELSE 0 END) as with_retries, "
        "SUM(retry_count) as total_retries "
        f"FROM tasks WHERE status IN ({_TERMINAL_PLACEHOLDERS}) "
        "GROUP BY model_tier",
        _TERMINAL_STATUSES,
    )
    retries_by_tier = [
        RetryByTier(
            model_tier=r["model_tier"],
            total_tasks=r["total"],
            tasks_with_retries=r["with_retries"] or 0,
            total_retries=r["total_retries"] or 0,
            retry_rate=round((r["with_retries"] or 0) / r["total"], 4) if r["total"] else 0,
        )
        for r in retry_rows
    ]

    # Checkpoint counts
    cp_row = await db.fetchone(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) as unresolved "
        "FROM checkpoints"
    )
    checkpoint_count = cp_row["total"] if cp_row else 0
    unresolved = (cp_row["unresolved"] or 0) if cp_row else 0

    # Wave throughput — per-project, terminal statuses with timing data
    wave_rows = await db.fetchall(
        "SELECT t.project_id, p.name as project_name, t.wave, "
        "COUNT(*) as task_count, "
        "AVG(t.completed_at - t.started_at) as avg_duration "
        "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id "
        f"WHERE t.status IN ({_TERMINAL_PLACEHOLDERS}) "
        "AND t.completed_at IS NOT NULL AND t.started_at IS NOT NULL "
        "GROUP BY t.project_id, t.wave ORDER BY t.project_id, t.wave",
        _TERMINAL_STATUSES,
    )
    wave_throughput = [
        WaveThroughput(
            project_id=r["project_id"],
            project_name=r["project_name"] or "(deleted)",
            wave=r["wave"],
            task_count=r["task_count"],
            avg_duration_seconds=round(r["avg_duration"], 2) if r["avg_duration"] else None,
        )
        for r in wave_rows
    ]

    # Cost efficiency by tier — terminal statuses only, costs from usage_log
    eff_rows = await db.fetchall(
        "SELECT t.model_tier, "
        "COALESCE(SUM(u.cost_usd), 0) as cost, "
        "COUNT(DISTINCT CASE WHEN t.status = ? THEN t.id END) as completed, "
        "COUNT(DISTINCT CASE WHEN t.verification_status = ? THEN t.id END) as passed "
        "FROM tasks t "
        "LEFT JOIN usage_log u ON u.task_id = t.id "
        f"WHERE t.status IN ({_TERMINAL_PLACEHOLDERS}) "
        "GROUP BY t.model_tier",
        (TaskStatus.COMPLETED.value, VerificationResult.PASSED.value, *_TERMINAL_STATUSES),
    )
    cost_efficiency = [
        CostEfficiencyItem(
            model_tier=r["model_tier"],
            cost_usd=round(r["cost"] or 0, 6),
            tasks_completed=r["completed"],
            verification_pass_count=r["passed"],
            cost_per_pass=round((r["cost"] or 0) / r["passed"], 6) if r["passed"] else None,
        )
        for r in eff_rows
    ]

    return AnalyticsEfficiency(
        retries_by_tier=retries_by_tier,
        checkpoint_count=checkpoint_count,
        unresolved_checkpoint_count=unresolved,
        wave_throughput=wave_throughput,
        cost_efficiency=cost_efficiency,
    )
