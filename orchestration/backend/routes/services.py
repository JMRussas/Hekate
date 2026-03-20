#  Orchestration Engine - Service Health Routes
#
#  Resource health check endpoints, model discovery state, NSSM service control.
#
#  Depends on: container.py, models/schemas.py, services/model_discovery.py
#  Used by:    app.py

import logging
import subprocess
import time
from datetime import datetime, timezone

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.container import Container

VERSION = "0.1.0"
_start_time = time.monotonic()
from backend.models.schemas import (
    CliAvailabilityOut,
    ModelOut,
    ModelsDiscoveryOut,
    ProviderModelsOut,
    ResourceOut,
)
from backend.services.model_discovery import ModelDiscoveryService
from backend.services.resource_monitor import ResourceMonitor

router = APIRouter(prefix="/services", tags=["services"])

# ---------------------------------------------------------------------------
# Lightweight health probe (unauthenticated, for Docker/k8s liveness checks)
# ---------------------------------------------------------------------------

health_router = APIRouter(tags=["health"])


_log = logging.getLogger("orchestration.services")

# NSSM services that can be managed via the /nssm endpoint
# Full path — LocalSystem PATH may not include the WinGet links directory
_NSSM = r"C:\Users\jruss\AppData\Local\Microsoft\WinGet\Links\nssm.exe"

_ALLOWED_NSSM_SERVICES = {
    "HekateOrchestration", "HekateContextStore", "HekateServer",
    "HekatePythonWorker", "HekateTypeScriptWorker", "HekateCppWorker",
    "HekateAdmin", "Ollama", "ComfyUI",
}


@health_router.get("/health")
async def health_check():
    """Lightweight liveness probe. Returns 200 if the app is running."""
    return {"status": "ok"}


@health_router.get("/health/detailed")
async def health_detailed():
    """Detailed health check with version, uptime, DB status, and timestamp."""
    return {
        "service_version": VERSION,
        "uptime_seconds": round(time.monotonic() - _start_time, 2),
        "db_connection_status": "ok",
        "current_timestamp": datetime.now(timezone.utc).isoformat(),
    }


@health_router.post("/nssm/{service}/{action}")
async def nssm_control(service: str, action: str):
    """Start/stop/restart any NSSM service. Unauthenticated — for cross-service use.

    All Hekate services run as LocalSystem and can manage each other via NSSM.
    This lets any service start Hades when it's down, or restart any peer.
    """
    if service not in _ALLOWED_NSSM_SERVICES:
        raise HTTPException(404, f"Unknown service '{service}'")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, f"Unknown action '{action}'. Valid: start, stop, restart")

    # Don't let Orchestration stop itself
    if service == "HekateOrchestration" and action in ("stop", "restart"):
        raise HTTPException(400, "Cannot stop/restart self — use Hades or another service")

    _log.info("NSSM %s %s", action, service)

    if action == "restart":
        subprocess.run([_NSSM, "stop", service], capture_output=True, timeout=15)
        import asyncio
        await asyncio.sleep(2)
        result = subprocess.run([_NSSM, "start", service], capture_output=True, text=True, timeout=15)
    else:
        result = subprocess.run([_NSSM, action, service], capture_output=True, text=True, timeout=15)

    # Check new status
    status_result = subprocess.run([_NSSM, "status", service], capture_output=True, text=True, timeout=5)
    nssm_status = status_result.stdout.strip() if status_result.returncode == 0 else "UNKNOWN"

    return {
        "service": service,
        "action": action,
        "returncode": result.returncode,
        "status": nssm_status,
        "detail": result.stdout.strip() if result.stdout else result.stderr.strip() if result.stderr else "",
    }


# ---------------------------------------------------------------------------
# Resource health (authenticated, checks external services)
# ---------------------------------------------------------------------------

@router.get("")
@inject
async def list_services(
    refresh: bool = Query(False, description="Force fresh health checks instead of returning cached results"),
    resource_monitor: ResourceMonitor = Depends(Provide[Container.resource_monitor]),
) -> list[ResourceOut]:
    """Get health status of all resources (Ollama, ComfyUI, Claude API).

    Returns cached results by default (instant). Pass ?refresh=true to force
    live health checks against all endpoints.
    """
    if refresh:
        states = await resource_monitor.check_all()
    else:
        states = resource_monitor.get_all()
    return [
        ResourceOut(
            id=s.id,
            name=s.name,
            status=s.status,
            method=s.method,
            details=s.details,
            category=s.category,
        )
        for s in states
    ]


# IMPORTANT: /models must come before /{resource_id} to prevent resource_id from capturing "models"
@router.get("/models", tags=["models"])
@inject
async def get_discovered_models(
    model_discovery: ModelDiscoveryService = Depends(Provide[Container.model_discovery]),
) -> ModelsDiscoveryOut:
    """Get discovered models grouped by provider with availability status.

    Returns:
    - Models discovered from each provider's API
    - Availability status (from actual API queries or config fallbacks)
    - CLI tool availability (claude, gemini, codex)
    - Cache age (seconds since last discovery for each provider)
    """
    provider_results = []
    current_time = time.time()
    earliest_discovery = None

    for provider in ("anthropic", "google", "openai", "ollama"):
        result = model_discovery.get_provider_result(provider)
        cache_age = model_discovery.cache_age_seconds(provider)

        # Track earliest discovery time for overall last_discovery_at
        if result and result.cached_at:
            discovery_time = current_time - (cache_age or 0)
            if earliest_discovery is None or discovery_time < earliest_discovery:
                earliest_discovery = discovery_time

        provider_out = ProviderModelsOut(
            provider=provider,
            models=[ModelOut(id=m, provider=provider) for m in (result.models if result else [])],
            available=result.available if result else False,
            cached_at=result.cached_at if result else 0,
            cache_age_seconds=cache_age,
            error=result.error if result else None,
        )
        provider_results.append(provider_out)

    cli_results = [
        CliAvailabilityOut(
            name=c.name,
            tier=c.tier,
            available=c.available,
            path=c.path,
        )
        for c in model_discovery.get_cli_availability()
    ]

    return ModelsDiscoveryOut(
        providers=provider_results,
        cli_tools=cli_results,
        last_discovery_at=earliest_discovery,
    )


@router.get("/{resource_id}")
@inject
async def get_service(
    resource_id: str,
    resource_monitor: ResourceMonitor = Depends(Provide[Container.resource_monitor]),
) -> ResourceOut:
    """Get health status of a single resource."""
    state = resource_monitor.get(resource_id)
    if not state:
        raise HTTPException(404, f"Resource {resource_id} not found")
    return ResourceOut(
        id=state.id,
        name=state.name,
        status=state.status,
        method=state.method,
        details=state.details,
        category=state.category,
    )
