#  Orchestration Engine - Sentinel Routes
#
#  REST endpoints for the Sentinel monitoring subsystem.
#  Exposes system/plan health status, observations, and intervention management.
#
#  Depends on: container.py, services/sentinel/system_sentinel.py, middleware/auth.py
#  Used by:    app.py

import asyncio
import json
import logging
from datetime import datetime, timezone

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.container import Container
from backend.middleware.auth import get_current_user, get_user_from_sse_token
from backend.services.sentinel.models import HealthState, Severity
from backend.services.sentinel.system_sentinel import SystemSentinel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sentinel", tags=["sentinel"])

# Map SentinelBus topics to SSE event names expected by clients.
_TOPIC_TO_SSE_EVENT: dict[str, str] = {
    "resource_alert": "health_update",
    "sentinel_heartbeat": "health_update",
    "stall_notification": "plan_observation",
    "contention_advisory": "plan_observation",
    "intervention_proposal": "intervention_proposal",
}


async def _sentinel_event_generator(sentinel: SystemSentinel):
    """Yield SSE-formatted strings from the SentinelBus."""
    sub = sentinel._bus.subscribe()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(sub._queue.get(), timeout=30.0)
                if msg is None:
                    break
                sse_event = _TOPIC_TO_SSE_EVENT.get(msg.topic, msg.topic)
                payload = {
                    "event": sse_event,
                    "topic": msg.topic,
                    "source": msg.source,
                    "message_id": msg.message_id,
                    "timestamp": msg.timestamp.isoformat(),
                    **msg.payload,
                }
                yield f"event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        sub.unsubscribe()


# ---------------------------------------------------------------------------
# GET /events — SSE stream for sentinel events
# ---------------------------------------------------------------------------

@router.get("/events")
@inject
async def stream_sentinel_events(
    user: dict = Depends(get_user_from_sse_token),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
) -> StreamingResponse:
    """SSE stream for sentinel events (health_update, plan_observation, intervention_proposal, intervention_result)."""
    return StreamingResponse(
        _sentinel_event_generator(sentinel),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# GET /status — overall sentinel system status
# ---------------------------------------------------------------------------

@router.get("/status")
@inject
async def get_status(
    current_user: dict = Depends(get_current_user),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
):
    """Return overall Sentinel system status: running state, health trends, active plan sentinels."""
    trends = sentinel.get_all_trends()
    plan_sentinels = sentinel.plan_sentinels

    trend_summaries = {}
    for resource_id, trend in trends.items():
        trend_summaries[resource_id] = {
            "state": trend.state.value,
            "previous_state": trend.previous_state.value if trend.previous_state else None,
            "failure_rate": round(trend.failure_rate, 3),
            "avg_latency_ms": round(trend.avg_latency_ms, 1) if trend.avg_latency_ms is not None else None,
            "sample_count": len(trend.samples),
        }

    plan_sentinel_summaries = {}
    for project_id, ps in plan_sentinels.items():
        plan_sentinel_summaries[project_id] = {
            "running": ps.running,
            "events_processed": ps.state.events_processed,
            "current_wave": ps.state.current_wave,
            "task_count": len(ps.state.task_statuses),
            "failure_count": sum(ps.state.failure_counts.values()),
        }

    return {
        "running": sentinel.running,
        "health_trends": trend_summaries,
        "plan_sentinels": plan_sentinel_summaries,
        "active_plan_sentinel_count": len(plan_sentinels),
    }


# ---------------------------------------------------------------------------
# GET /observations — filterable list of recent observations
# ---------------------------------------------------------------------------

@router.get("/observations")
@inject
async def list_observations(
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    category: str | None = Query(default=None, description="Filter by category (e.g. task_stuck, cascade_failure)"),
    severity: str | None = Query(default=None, description="Filter by severity (info, warning, critical)"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
    db=Depends(Provide[Container.db]),
):
    """Return observations from the durable store, with optional filtering.

    Queries the sentinel_observations table so observations survive
    PlanSentinel teardown. Falls back to in-memory if DB unavailable.
    """
    if severity is not None:
        valid_severities = {s.value for s in Severity}
        if severity not in valid_severities:
            raise HTTPException(400, f"Invalid severity '{severity}'. Must be one of: {', '.join(sorted(valid_severities))}")

    # Build query with filters
    sql = "SELECT id, project_id, task_id, category, severity, message, details_json, created_at FROM sentinel_observations WHERE 1=1"
    params: list = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    if severity is not None:
        sql += " AND severity = ?"
        params.append(severity)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    try:
        rows = await db.fetchall(sql, tuple(params))
    except Exception:
        rows = []

    observations = []
    for row in rows:
        details = None
        if row["details_json"]:
            try:
                details = json.loads(row["details_json"])
            except (json.JSONDecodeError, TypeError):
                details = row["details_json"]
        observations.append({
            "observation_id": row["id"],
            "category": row["category"],
            "message": row["message"],
            "severity": row["severity"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "details": details,
            "timestamp": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
        })

    return observations


# ---------------------------------------------------------------------------
# GET /interventions — list intervention proposals (optionally filtered by status)
# ---------------------------------------------------------------------------

@router.get("/interventions")
@inject
async def list_interventions(
    status: str | None = Query(default=None, description="Filter by status: pending, approved, rejected"),
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
):
    """Return intervention proposals from the sentinel bus history.

    Collects interventions from active Plan Sentinel state — handled_interventions
    tracks what's been acted on, while the bus carries proposal payloads.
    """
    interventions = []

    for pid, ps in sentinel.plan_sentinels.items():
        for obs in ps._observation_history:
            # Only include observations that map to interventions
            from backend.services.sentinel.plan_sentinel import _CATEGORY_TO_INTERVENTION
            entry = _CATEGORY_TO_INTERVENTION.get(obs.category)
            if entry is None:
                continue

            action, tier = entry
            dedup_target = obs.task_id or str(obs.details.get("wave", ""))
            dedup_key = (action.value, dedup_target)
            is_handled = dedup_key in ps.state.handled_interventions

            intervention = {
                "id": obs.observation_id,
                "action": action.value,
                "tier": tier.value,
                "status": "executed" if is_handled else "pending",
                "category": obs.category,
                "severity": obs.severity.value,
                "message": obs.message,
                "project_id": obs.project_id,
                "task_id": obs.task_id,
                "details": obs.details,
                "timestamp": obs.timestamp.isoformat() if isinstance(obs.timestamp, datetime) else str(obs.timestamp),
            }
            interventions.append(intervention)

    # Apply filters
    if status is not None:
        interventions = [i for i in interventions if i["status"] == status]
    if project_id is not None:
        interventions = [i for i in interventions if i["project_id"] == project_id]

    # Sort by timestamp descending
    interventions.sort(key=lambda i: i["timestamp"], reverse=True)

    return interventions[:limit]


# ---------------------------------------------------------------------------
# POST /interventions/{id}/approve — approve a pending intervention
# ---------------------------------------------------------------------------

@router.post("/interventions/{intervention_id}/approve")
@inject
async def approve_intervention(
    intervention_id: str,
    current_user: dict = Depends(get_current_user),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
):
    """Approve a pending supervised intervention and trigger execution."""
    # Find the observation across all plan sentinels
    for pid, ps in sentinel.plan_sentinels.items():
        for obs in ps._observation_history:
            if obs.observation_id != intervention_id:
                continue

            from backend.services.sentinel.plan_sentinel import _CATEGORY_TO_INTERVENTION
            entry = _CATEGORY_TO_INTERVENTION.get(obs.category)
            if entry is None:
                raise HTTPException(400, "Observation does not map to an intervention")

            action, tier = entry

            # Execute the intervention via the plan sentinel
            await ps._execute_auto_intervention(action, obs)

            logger.info(
                "Intervention %s approved by user %s for project %s",
                intervention_id[:8], current_user.get("username", "?"), pid,
            )

            return {
                "id": intervention_id,
                "action": action.value,
                "status": "approved",
                "project_id": pid,
            }

    raise HTTPException(404, f"Intervention {intervention_id} not found")


# ---------------------------------------------------------------------------
# POST /interventions/{id}/reject — reject a pending intervention
# ---------------------------------------------------------------------------

@router.post("/interventions/{intervention_id}/reject")
@inject
async def reject_intervention(
    intervention_id: str,
    current_user: dict = Depends(get_current_user),
    sentinel: SystemSentinel = Depends(Provide[Container.system_sentinel]),
):
    """Reject a pending supervised intervention — marks it as handled without executing."""
    for pid, ps in sentinel.plan_sentinels.items():
        for obs in ps._observation_history:
            if obs.observation_id != intervention_id:
                continue

            from backend.services.sentinel.plan_sentinel import _CATEGORY_TO_INTERVENTION
            entry = _CATEGORY_TO_INTERVENTION.get(obs.category)
            if entry is None:
                raise HTTPException(400, "Observation does not map to an intervention")

            action, tier = entry

            # Mark as handled so it won't be proposed again
            dedup_target = obs.task_id or str(obs.details.get("wave", ""))
            dedup_key = (action.value, dedup_target)
            ps.state.handled_interventions.add(dedup_key)

            logger.info(
                "Intervention %s rejected by user %s for project %s",
                intervention_id[:8], current_user.get("username", "?"), pid,
            )

            return {
                "id": intervention_id,
                "action": action.value,
                "status": "rejected",
                "project_id": pid,
            }

    raise HTTPException(404, f"Intervention {intervention_id} not found")
