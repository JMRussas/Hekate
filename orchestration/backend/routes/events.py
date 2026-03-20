#  Orchestration Engine - SSE Event Routes
#
#  Server-Sent Events streaming for real-time progress.
#  Uses short-lived SSE tokens scoped to a single project.
#  Also provides a unified stream endpoint for the Iris desktop app.
#
#  Depends on: container.py, services/progress.py, services/auth.py, middleware/auth.py
#  Used by:    app.py

import logging

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.services.auth import AuthService
from backend.services.progress import ProgressManager

logger = logging.getLogger("orchestration.events")

router = APIRouter(prefix="/events", tags=["events"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    # Defense-in-depth: also set by SecurityHeadersMiddleware, but
    # kept here in case middleware is ever removed or reordered.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _is_localhost(request: Request) -> bool:
    """Check if the request originates from localhost."""
    if not request.client:
        return False
    host = request.client.host
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/stream")
@inject
async def stream_all_events(
    request: Request,
    progress: ProgressManager = Depends(Provide[Container.progress]),
):
    """Unified SSE stream for all projects/tasks.

    Used by the Iris desktop app for real-time updates.
    No auth required for localhost connections.
    Emits events with types: token, phase, tool_call, tool_result,
    done, status, output, error.
    """
    if not _is_localhost(request):
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unified stream is only available from localhost",
        )

    logger.info("Iris SSE client connected from %s", request.client.host)
    return StreamingResponse(
        progress.subscribe_all(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/{project_id}/token")
@inject
async def create_sse_token(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
    auth: AuthService = Depends(Provide[Container.auth]),
):
    """Issue a short-lived SSE token scoped to a single project."""
    from backend.routes.projects import _get_owned_project
    await _get_owned_project(db, project_id, current_user)
    token = auth.create_sse_token(current_user["id"], project_id)
    return {"token": token}


@router.get("/{project_id}")
@inject
async def stream_project_events(
    project_id: str,
    request: Request,
    token: str = Query(default=None),
    progress: ProgressManager = Depends(Provide[Container.progress]),
    auth: AuthService = Depends(Provide[Container.auth]),
):
    """SSE stream for project-level events (task starts, completions, budget warnings).

    Auth is required via SSE token query parameter, unless the request
    originates from localhost (dev mode bypass for Iris).
    """
    if not _is_localhost(request):
        # Non-localhost: require SSE token auth
        if not token:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SSE token required (pass as ?token=...)",
            )
        # Validate token inline (mirrors get_user_from_sse_token logic)
        from backend.middleware.auth import _validate_token
        user, payload = await _validate_token(auth, token, "sse")
        if payload.get("project_id") != project_id:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="SSE token not valid for this project",
            )

    return StreamingResponse(
        progress.subscribe(project_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
