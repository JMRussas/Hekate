#  Orchestration Engine - Metrics Routes
#
#  Daily metrics query endpoints for analytics dashboards.
#
#  Depends on: container.py, db/connection.py, middleware/auth.py
#  Used by:    app.py

import json
from datetime import date, timedelta

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, Query

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/daily")
@inject
async def get_daily_metrics(
    days: int = Query(default=7, ge=1, le=90),
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[dict]:
    """Daily metrics grouped by date."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = await db.fetchall(
        "SELECT date, metric_name, metric_value, details_json "
        "FROM daily_metrics WHERE date >= $1 ORDER BY date, metric_name",
        (since,),
    )
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        d = r["date"]
        entry = {
            "name": r["metric_name"],
            "value": r["metric_value"],
            "details": json.loads(r["details_json"]) if r["details_json"] else None,
        }
        grouped.setdefault(d, []).append(entry)
    return [{"date": d, "metrics": m} for d, m in grouped.items()]


@router.get("/errors")
@inject
async def get_error_metrics(
    days: int = Query(default=1, ge=1, le=90),
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
) -> list[dict]:
    """Error metrics (metric_name starting with 'error')."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = await db.fetchall(
        "SELECT date, metric_name, metric_value, details_json "
        "FROM daily_metrics WHERE metric_name LIKE 'error%' AND date >= $1 "
        "ORDER BY date DESC, metric_name",
        (since,),
    )
    return [
        {
            "date": r["date"],
            "metric_name": r["metric_name"],
            "value": r["metric_value"],
            "details": json.loads(r["details_json"]) if r["details_json"] else None,
        }
        for r in rows
    ]
