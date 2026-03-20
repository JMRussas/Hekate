#  Orchestration Engine - Sentinel Routes
#
#  REST endpoints for observations, interventions, and decision audit trail.
#  The legacy SystemSentinel/PlanSentinel have been removed — Odin handles
#  monitoring. These routes provide read access to historical data and
#  the SSE event stream from the bus.
#
#  Depends on: container.py, services/sentinel/bus.py, services/sentinel/decision_logger.py
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
from backend.services.sentinel.decision_logger import DecisionLogger
from backend.services.sentinel.models import Severity

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


async def _bus_event_generator(bus):
    """Yield SSE-formatted strings from the SentinelBus."""
    sub = bus.subscribe()
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
# GET /events — SSE stream from the bus (Odin + any publisher)
# ---------------------------------------------------------------------------

@router.get("/events")
@inject
async def stream_sentinel_events(
    user: dict = Depends(get_user_from_sse_token),
    bus=Depends(Provide[Container.sentinel_bus]),
) -> StreamingResponse:
    """SSE stream for bus events (health_update, plan_observation, intervention_proposal, etc.)."""
    return StreamingResponse(
        _bus_event_generator(bus),
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
# GET /status — monitoring status (Odin-era: just report bus is alive)
# ---------------------------------------------------------------------------

@router.get("/status")
@inject
async def get_status(
    current_user: dict = Depends(get_current_user),
):
    """Return monitoring status. Legacy sentinel removed — Odin handles monitoring via /odin/status."""
    return {
        "engine": "odin",
        "legacy_sentinel": "removed",
        "note": "Use GET /odin/status for Odin overseer status",
    }


# ---------------------------------------------------------------------------
# GET /observations — filterable list of observations
# ---------------------------------------------------------------------------

@router.get("/observations")
@inject
async def list_observations(
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    category: str | None = Query(default=None, description="Filter by category (e.g. task_stuck, cascade_failure)"),
    severity: str | None = Query(default=None, description="Filter by severity (info, warning, critical)"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db=Depends(Provide[Container.db]),
):
    """Return observations from the sentinel_observations table."""
    if severity is not None:
        valid_severities = {s.value for s in Severity}
        if severity not in valid_severities:
            raise HTTPException(400, f"Invalid severity '{severity}'. Must be one of: {', '.join(sorted(valid_severities))}")

    sql = "SELECT id, project_id, task_id, category, severity, message, details_json, created_at FROM sentinel_observations WHERE 1=1"
    params: list = []
    param_idx = 0
    if project_id is not None:
        param_idx += 1
        sql += f" AND project_id = ${param_idx}"
        params.append(project_id)
    if category is not None:
        param_idx += 1
        sql += f" AND category = ${param_idx}"
        params.append(category)
    if severity is not None:
        param_idx += 1
        sql += f" AND severity = ${param_idx}"
        params.append(severity)
    param_idx += 1
    sql += f" ORDER BY created_at DESC LIMIT ${param_idx}"
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
# GET /interventions — list intervention proposals
# ---------------------------------------------------------------------------

@router.get("/interventions")
@inject
async def list_interventions(
    status: str | None = Query(default=None, description="Filter by status: pending, approved, rejected, executed"),
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db=Depends(Provide[Container.db]),
):
    """Return intervention proposals and results from the durable store."""
    sql = (
        "SELECT id, project_id, task_id, category, severity, message, details_json, created_at "
        "FROM sentinel_observations "
        "WHERE category IN ('intervention_proposal', 'intervention_result')"
    )
    params: list = []

    if project_id is not None:
        sql += " AND project_id = $1"
        params.append(project_id)
    sql += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
    params.append(limit)

    try:
        rows = await db.fetchall(sql, tuple(params))
    except Exception:
        logger.exception("Failed to query interventions from DB")
        rows = []

    interventions = []
    for row in rows:
        details = {}
        if row["details_json"]:
            try:
                details = json.loads(row["details_json"])
            except (json.JSONDecodeError, TypeError):
                details = {}

        if row["category"] == "intervention_result":
            row_status = "executed"
        else:
            row_status = details.get("status", "pending")

        intervention = {
            "id": row["id"],
            "action": details.get("action", "unknown"),
            "tier": details.get("tier", "unknown"),
            "status": row_status,
            "category": row["category"],
            "severity": row["severity"],
            "message": row["message"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "details": details,
            "timestamp": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
        }
        interventions.append(intervention)

    if status is not None:
        interventions = [i for i in interventions if i["status"] == status]

    return interventions


# ---------------------------------------------------------------------------
# POST /interventions/{id}/approve
# ---------------------------------------------------------------------------

@router.post("/interventions/{intervention_id}/approve")
@inject
async def approve_intervention(
    intervention_id: str,
    current_user: dict = Depends(get_current_user),
    bus=Depends(Provide[Container.sentinel_bus]),
    db=Depends(Provide[Container.db]),
):
    """Approve a pending intervention and forward to Odin via the bus."""
    row = await db.fetchone(
        "SELECT id, project_id, task_id, category, severity, message, details_json, created_at "
        "FROM sentinel_observations WHERE id = $1 AND category = 'intervention_proposal'",
        (intervention_id,),
    )
    if row is None:
        raise HTTPException(404, f"Intervention {intervention_id} not found")

    details = {}
    if row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    if details.get("status") in ("approved", "rejected"):
        raise HTTPException(400, f"Intervention already {details['status']}")

    action_str = details.get("action")
    if not action_str:
        raise HTTPException(400, "Intervention record missing action")

    pid = row["project_id"]

    from backend.services.sentinel.models import SentinelMessage
    await bus.publish(SentinelMessage(
        topic="odin_anomaly",
        source="sentinel_route",
        payload={
            "type": "intervention_approved",
            "action": action_str,
            "project_id": pid,
            "task_id": row["task_id"],
            "severity": row["severity"],
            "message": row["message"],
            "details": details,
            "intervention_id": intervention_id,
        },
    ))

    details["status"] = "approved"
    details["approved_by"] = current_user.get("username", "unknown")
    details["approved_at"] = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE sentinel_observations SET details_json = $1 WHERE id = $2",
        (json.dumps(details), intervention_id),
    )

    return {"id": intervention_id, "action": action_str, "status": "approved", "project_id": pid}


# ---------------------------------------------------------------------------
# POST /interventions/{id}/reject
# ---------------------------------------------------------------------------

@router.post("/interventions/{intervention_id}/reject")
@inject
async def reject_intervention(
    intervention_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(Provide[Container.db]),
):
    """Reject a pending intervention."""
    row = await db.fetchone(
        "SELECT id, project_id, task_id, category, details_json "
        "FROM sentinel_observations WHERE id = $1 AND category = 'intervention_proposal'",
        (intervention_id,),
    )
    if row is None:
        raise HTTPException(404, f"Intervention {intervention_id} not found")

    details = {}
    if row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    if details.get("status") in ("approved", "rejected"):
        raise HTTPException(400, f"Intervention already {details['status']}")

    action_str = details.get("action", "unknown")
    pid = row["project_id"]

    details["status"] = "rejected"
    details["rejected_by"] = current_user.get("username", "unknown")
    details["rejected_at"] = datetime.now(timezone.utc).isoformat()
    await db.execute_write(
        "UPDATE sentinel_observations SET details_json = $1 WHERE id = $2",
        (json.dumps(details), intervention_id),
    )

    return {"id": intervention_id, "action": action_str, "status": "rejected", "project_id": pid}


# ---------------------------------------------------------------------------
# GET /decisions — audit trail
# ---------------------------------------------------------------------------

@router.get("/decisions")
@inject
async def list_decisions(
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    command: str | None = Query(default=None, description="Filter by command type"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db=Depends(Provide[Container.db]),
):
    """Return decision audit trail (Odin + historical sentinel decisions)."""
    decision_logger = DecisionLogger(db)

    if project_id:
        records = await decision_logger.query_decisions(
            project_id=project_id, limit=limit, command=command,
        )
    elif command:
        records = await decision_logger.query_similar_decisions(
            command=command, limit=limit,
        )
    else:
        try:
            rows = await db.fetchall(
                """SELECT decision_id, project_id, created_at, decision_type,
                          reasoning, confidence, outcome, details_json
                   FROM odin_decisions
                   ORDER BY created_at DESC LIMIT $1""",
                (limit,),
            )
            records = [DecisionLogger._row_to_record(r) for r in rows]
        except Exception:
            logger.debug("Failed to query all decisions", exc_info=True)
            records = []

    return [
        {
            "decision_id": r.id,
            "project_id": r.project_id,
            "timestamp": r.timestamp.isoformat(),
            "command": r.command.value,
            "reasoning": r.reasoning,
            "confidence": r.confidence,
            "outcome": r.outcome,
            "details": getattr(r, "details", {}),
        }
        for r in records
    ]
