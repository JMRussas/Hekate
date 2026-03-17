#  Orchestration Engine - FastAPI Application
#
#  Main app setup: lifespan, CORS, router includes.
#  Creates the DI container and manages service lifecycle.
#
#  Depends on: config.py, container.py, routes/*.py, middleware/auth.py
#  Used by:    run.py

import logging
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from backend.config import CORS_ORIGINS, DB_PATH, PROJECT_ROOT, cfg, validate_config
from backend.container import Container
from backend.exceptions import (
    AccountLinkError,
    BudgetExhaustedError,
    CycleDetectedError,
    GitError,
    InvalidStateError,
    NotFoundError,
    OIDCError,
    OrchestrationError,
    PlanParseError,
)
from backend.logging_config import set_request_id
from backend.middleware.auth import get_current_user
from backend.rate_limit import limiter
from backend.services.model_router import set_discovery_service
from backend.routes.admin import router as admin_router
from backend.routes.analytics import router as analytics_router
from backend.routes.auth import router as auth_router
from backend.routes.auth_oidc import router as auth_oidc_router
from backend.routes.checkpoints import router as checkpoints_router
from backend.routes.events import router as events_router
from backend.routes.external import router as external_router
from backend.routes.chat import router as chat_router
from backend.routes.internal import router as internal_router
from backend.routes.projects import router as projects_router
from backend.routes.rag import router as rag_router
from backend.routes.sentinel import router as sentinel_router
from backend.routes.services import health_router, router as services_router
from backend.routes.tasks import router as tasks_router
from backend.routes.usage import router as usage_router

logger = logging.getLogger("orchestration.app")

# Create and wire the DI container
container = Container()

# Auth dependency for all protected routes
_auth_dep = [Depends(get_current_user)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle.

    Uses AsyncExitStack so that if any startup step fails, all previously
    initialized resources are cleaned up in reverse order.
    """
    logger.info("Orchestration Engine starting...")

    # Validate critical config before anything else
    validate_config()

    # Get instances from container
    db = container.db()
    http_client = container.http_client()
    resource_monitor = container.resource_monitor()
    model_discovery = container.model_discovery()
    executor = container.executor()
    system_sentinel = container.system_sentinel()

    async with AsyncExitStack() as stack:
        logger.info("Initializing database...")
        # ORCHESTRATION_DSN env var → Postgres; otherwise → SQLite at DB_PATH
        dsn = os.environ.get("ORCHESTRATION_DSN")
        await db.init(dsn or DB_PATH, run_migrations=True)
        stack.push_async_callback(db.close)
        logger.info("Database initialized")

        # Shared httpx client — close on shutdown
        stack.push_async_callback(http_client.aclose)

        logger.info("Starting resource monitor...")
        await resource_monitor.start_background()
        stack.push_async_callback(resource_monitor.stop_background)
        logger.info("Resource monitor started")

        # Discover available models from provider APIs
        await model_discovery.discover()
        set_discovery_service(model_discovery)  # Wire into model_router module
        stack.push_async_callback(model_discovery.close)
        logger.info("Model discovery completed")

        # Sentinel engine: "odin" (LLM-driven) or "legacy" (rule-based)
        sentinel_engine = cfg("sentinel.engine", "odin")
        if sentinel_engine == "odin":
            odin = container.odin()
            await odin.start()
            stack.push_async_callback(odin.stop)
            logger.info("Odin overseer started (model=%s)", cfg("odin.model", "qwen3.5:latest"))
        else:
            await system_sentinel.start()
            stack.push_async_callback(system_sentinel.stop)
            logger.info("Legacy sentinel started")

        await executor.start()
        stack.push_async_callback(executor.stop)

        # Register graceful shutdown: pause projects, wait for running tasks
        async def _graceful_shutdown():
            logger.info("Graceful shutdown: pausing executing projects...")
            import time as _time
            now = _time.time()
            await db.execute_write(
                "UPDATE projects SET status = 'paused', updated_at = $1 WHERE status = 'executing'",
                (now,),
            )
            # Wait up to 30s for running tasks to finish
            for i in range(15):
                row = await db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks WHERE status IN ('running', 'claimed')", ()
                )
                active = row["cnt"] if row else 0
                if active == 0:
                    break
                logger.info("Graceful shutdown: waiting for %d running tasks... (%d/15)", active, i + 1)
                await asyncio.sleep(2)
            # Release any still-claimed tasks
            await db.execute_write(
                "UPDATE tasks SET status = 'pending', claimed_by = NULL, claimed_at = NULL, updated_at = $1 "
                "WHERE status = 'claimed'",
                (now,),
            )
            logger.info("Graceful shutdown: all projects paused, ready to stop")

        stack.push_async_callback(_graceful_shutdown)

        # Register auto-resume on startup for next boot
        paused = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM projects WHERE status = 'paused'", ()
        )
        if paused and paused["cnt"] > 0:
            import time as _time
            now = _time.time()
            result = await db.execute_write(
                "UPDATE projects SET status = 'executing', updated_at = $1 WHERE status = 'paused'",
                (now,),
            )
            logger.info("Auto-resumed %s paused projects from previous shutdown", result)

        yield

    logger.info("Orchestration Engine shutting down")


app = FastAPI(
    title="Orchestration Engine",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."},
    )


# Global exception handlers — safety net for uncaught business errors
@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(BudgetExhaustedError)
async def budget_handler(request: Request, exc: BudgetExhaustedError):
    return JSONResponse(status_code=402, content={"detail": str(exc)})


@app.exception_handler(PlanParseError)
async def plan_parse_handler(request: Request, exc: PlanParseError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(CycleDetectedError)
async def cycle_handler(request: Request, exc: CycleDetectedError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(InvalidStateError)
async def invalid_state_handler(request: Request, exc: InvalidStateError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(OIDCError)
async def oidc_error_handler(request: Request, exc: OIDCError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(AccountLinkError)
async def account_link_handler(request: Request, exc: AccountLinkError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(GitError)
async def git_error_handler(request: Request, exc: GitError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(OrchestrationError)
async def orchestration_handler(request: Request, exc: OrchestrationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Raw ASGI middleware — avoids BaseHTTPMiddleware's response-buffering
# behavior that breaks SSE streaming.

class RequestIDMiddleware:
    """Inject X-Request-ID header and set contextvars request ID."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:12]
        set_request_id(rid)

        async def send_with_rid(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_rid)
        finally:
            set_request_id(None)


class SecurityHeadersMiddleware:
    """Inject security headers on all HTTP responses.

    CSP intentionally omitted: Vite build includes an inline theme-detection
    script that script-src 'self' would block.
    """

    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_names = {h[0] for h in headers}
                for name, value in self._HEADERS:
                    if name not in existing_names:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Health check (public, unauthenticated — for Docker/k8s liveness probes)
app.include_router(health_router, prefix="/api")

# Auth routes (public — no token required)
app.include_router(auth_router, prefix="/api")

# OIDC auth routes (mixed: public endpoints + authenticated link/unlink)
app.include_router(auth_oidc_router, prefix="/api")

# Protected API routes (require valid JWT)
app.include_router(projects_router, prefix="/api", dependencies=_auth_dep)
app.include_router(services_router, prefix="/api", dependencies=_auth_dep)
app.include_router(tasks_router, prefix="/api", dependencies=_auth_dep)
app.include_router(usage_router, prefix="/api", dependencies=_auth_dep)
app.include_router(checkpoints_router, prefix="/api", dependencies=_auth_dep)
app.include_router(admin_router, prefix="/api", dependencies=_auth_dep)
app.include_router(analytics_router, prefix="/api", dependencies=_auth_dep)
app.include_router(rag_router, prefix="/api", dependencies=_auth_dep)
app.include_router(external_router, prefix="/api", dependencies=_auth_dep)

# Chat routes — no auth for now (universal chat)
app.include_router(chat_router, prefix="/api")

# Internal routes — no auth (trusted by network isolation)
app.include_router(internal_router, prefix="/api")

# Events route uses query-param token auth (EventSource can't send headers)
app.include_router(events_router, prefix="/api")

# Sentinel routes — SSE endpoint uses query-param token auth, REST endpoints use per-route auth
app.include_router(sentinel_router, prefix="/api")

# Serve frontend build if available (SPA catch-all for client-side routing)
frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if frontend_dist.exists():
    from starlette.responses import FileResponse

    # Serve static assets (JS, CSS, images) directly
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="static-assets")

    # SPA catch-all: any non-API route serves index.html for client-side routing
    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        return FileResponse(str(frontend_dist / "index.html"))
