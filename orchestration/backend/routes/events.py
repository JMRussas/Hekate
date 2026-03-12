#  Orchestration Engine - SSE Event Routes
#
#  Server-Sent Events streaming for real-time progress.
#  Uses short-lived SSE tokens scoped to a single project.
#
#  Depends on: container.py, services/progress.py, services/auth.py, middleware/auth.py
#  Used by:    app.py

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user, get_user_from_sse_token
from backend.services.auth import AuthService
from backend.services.progress import ProgressManager

router = APIRouter(prefix="/events", tags=["events"])


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
    user: dict = Depends(get_user_from_sse_token),
    progress: ProgressManager = Depends(Provide[Container.progress]),
):
    """SSE stream for project-level events (task starts, completions, budget warnings)."""
    return StreamingResponse(
        progress.subscribe(project_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            # Defense-in-depth: also set by SecurityHeadersMiddleware, but
            # kept here in case middleware is ever removed or reordered.
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
