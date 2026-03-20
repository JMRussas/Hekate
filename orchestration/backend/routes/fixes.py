#  Orchestration Engine - Fix Queue Routes
#
#  Endpoints for listing, viewing, claiming, and resolving fix items.
#  Any authenticated user can view; admin can resolve.
#
#  Depends on: container.py, middleware/auth.py, services/fix_queue.py
#  Used by:    app.py

import json

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.services.fix_queue import FixQueue

router = APIRouter(prefix="/fixes", tags=["fixes"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class FileFixRequest(BaseModel):
    source: str = Field(default="human", description="Who filed this")
    category: str = Field(..., description="model_failure, infra_bug, code_bug, config, pattern, performance, data_quality")
    severity: str = Field(default="medium", description="low, medium, high, critical")
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=10)
    proposed_fix: str | None = None
    affected_component: str | None = None
    project_id: str | None = None
    task_id: str | None = None


class ResolveFixRequest(BaseModel):
    resolution: str = Field(..., min_length=5, description="What was done to fix it")
    resolved_by: str = Field(default="human")


class WontFixRequest(BaseModel):
    reason: str = Field(..., min_length=5, description="Why this won't be fixed")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
@inject
async def list_fixes(
    status: str = Query(default="open", description="open, claimed, in_progress, resolved, wont_fix, all"),
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    component: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """List fix queue items."""
    fq = FixQueue(db)

    if status == "all":
        rows = await db.fetchall(
            "SELECT * FROM fix_queue ORDER BY created_at DESC LIMIT $1",
            (limit,),
        )
        return [_row_to_fix(r) for r in rows]

    return await fq.list_open(
        category=category,
        severity=severity,
        component=component,
        limit=limit,
    )


@router.get("/summary")
@inject
async def fix_summary(
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Get fix queue status counts."""
    fq = FixQueue(db)
    counts = await fq.count_by_status()
    return {
        "counts": counts,
        "total_open": counts.get("open", 0) + counts.get("claimed", 0) + counts.get("in_progress", 0),
        "total_resolved": counts.get("resolved", 0),
    }


@router.get("/{fix_id}")
@inject
async def get_fix(
    fix_id: str,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Get a single fix item."""
    fq = FixQueue(db)
    item = await fq.get(fix_id)
    if not item:
        raise HTTPException(404, f"Fix {fix_id} not found")
    item["evidence"] = json.loads(item.get("evidence_json") or "[]")
    return item


@router.post("")
@inject
async def file_fix(
    req: FileFixRequest,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """File a new fix item."""
    fq = FixQueue(db)
    item_id = await fq.file(
        source=req.source,
        category=req.category,
        severity=req.severity,
        title=req.title,
        description=req.description,
        proposed_fix=req.proposed_fix,
        affected_component=req.affected_component,
        project_id=req.project_id,
        task_id=req.task_id,
    )
    return {"id": item_id, "status": "filed"}


@router.post("/{fix_id}/claim")
@inject
async def claim_fix(
    fix_id: str,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Claim a fix item to work on it."""
    fq = FixQueue(db)
    ok = await fq.claim(fix_id, claimed_by=_user.get("email", "unknown"))
    if not ok:
        raise HTTPException(409, "Fix is not in 'open' status")
    return {"status": "claimed"}


@router.post("/{fix_id}/resolve")
@inject
async def resolve_fix(
    fix_id: str,
    req: ResolveFixRequest,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Resolve a fix with what was done. Feeds back into diagnostic RAG."""
    fq = FixQueue(db)
    ok = await fq.resolve(fix_id, resolution=req.resolution, resolved_by=req.resolved_by)
    if not ok:
        raise HTTPException(409, "Fix is not in an active status")
    return {"status": "resolved"}


@router.post("/{fix_id}/wont-fix")
@inject
async def wont_fix(
    fix_id: str,
    req: WontFixRequest,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Mark a fix as won't fix with reason."""
    fq = FixQueue(db)
    ok = await fq.wont_fix(fix_id, reason=req.reason)
    if not ok:
        raise HTTPException(409, "Fix is not in an active status")
    return {"status": "wont_fix"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_fix(row) -> dict:
    d = dict(row)
    d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
    return d
