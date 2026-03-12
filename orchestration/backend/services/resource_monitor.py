#  Orchestration Engine - Resource Monitor
#
#  Health checks for Ollama, ComfyUI, and Claude API.
#  Runs periodic background checks and caches results.
#  All I/O is async (httpx + asyncio) — never blocks the event loop.
#
#  Depends on: backend/config.py
#  Used by:    container.py, routes/services.py, services/executor.py

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from backend.config import (
    ANTHROPIC_API_KEY,
    COMFYUI_HOSTS,
    OLLAMA_HOSTS,
    RESOURCE_CHECK_INTERVAL,
)
from backend.models.enums import ResourceStatus

logger = logging.getLogger("orchestration.resource_monitor")


@dataclass
class ResourceDef:
    id: str
    name: str
    host: str
    port: int
    health_url: str | None
    category: str = "ai"


@dataclass
class ResourceState:
    id: str
    name: str
    status: ResourceStatus = ResourceStatus.OFFLINE
    method: str = ""
    details: dict = field(default_factory=dict)
    category: str = "ai"


# ---------------------------------------------------------------------------
# Resource definitions (built from config)
# ---------------------------------------------------------------------------

def _build_resources() -> list[ResourceDef]:
    """Build resource definitions from config values."""
    resources = []

    # Ollama hosts
    for key, url in OLLAMA_HOSTS.items():
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434
        label = f"Ollama ({key})"
        resources.append(ResourceDef(
            id=f"ollama_{key}",
            name=label,
            host=host,
            port=port,
            health_url=f"{url}/api/tags",
            category="ai",
        ))

    # ComfyUI hosts
    for key, url in COMFYUI_HOSTS.items():
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8188
        label = f"ComfyUI ({key})"
        resources.append(ResourceDef(
            id=f"comfyui_{key}",
            name=label,
            host=host,
            port=port,
            health_url=f"{url}/system_stats",
            category="ai",
        ))

    # Claude API
    resources.append(ResourceDef(
        id="anthropic_api",
        name="Claude API",
        host="api.anthropic.com",
        port=443,
        health_url=None,
        category="api",
    ))

    # Gemini CLI
    resources.append(ResourceDef(
        id="gemini_cli",
        name="Gemini CLI",
        host="",
        port=0,
        health_url=None,
        category="cli",
    ))

    # Codex CLI
    resources.append(ResourceDef(
        id="codex_cli",
        name="Codex CLI",
        host="",
        port=0,
        health_url=None,
        category="cli",
    ))

    return resources


# ---------------------------------------------------------------------------
# Health check helpers (async)
# ---------------------------------------------------------------------------

async def _check_tcp(host: str, port: int, timeout: float = 1.5) -> bool:
    """Async TCP connection check."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (ConnectionRefusedError, TimeoutError, OSError, asyncio.TimeoutError):
        return False


async def _check_http(
    url: str, client: httpx.AsyncClient, timeout: float = 2.0
) -> tuple[bool, dict]:
    """Async HTTP health check. Returns (ok, parsed_json_or_empty)."""
    try:
        resp = await client.get(url, timeout=timeout)
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except Exception:
                data = {}
            return True, data
    except Exception:
        pass
    return False, {}


async def _check_resource(
    res: ResourceDef, client: httpx.AsyncClient
) -> ResourceState:
    """Check a single resource's health (async)."""
    state = ResourceState(id=res.id, name=res.name, category=res.category)

    # CLI tools: check if binary is on PATH
    if res.category == "cli":
        cli_binaries = {"gemini_cli": "gemini", "codex_cli": "codex"}
        binary = cli_binaries.get(res.id)
        if binary and shutil.which(binary):
            state.status = ResourceStatus.ONLINE
            state.method = "cli"
            state.details = {"binary": binary, "path": shutil.which(binary)}
        else:
            state.status = ResourceStatus.OFFLINE
            state.method = "cli"
            state.details = {"binary": binary, "hint": f"Install {binary} CLI"}
        return state

    # Claude API: check if key is configured
    if res.id == "anthropic_api":
        if ANTHROPIC_API_KEY:
            state.status = ResourceStatus.ONLINE
            state.method = "api_key"
            state.details = {"key_configured": True}
        else:
            state.status = ResourceStatus.OFFLINE
            state.method = "api_key"
            state.details = {"key_configured": False, "hint": "Set ANTHROPIC_API_KEY env var"}
        return state

    # HTTP check
    if res.health_url:
        ok, data = await _check_http(res.health_url, client)
        if ok:
            state.status = ResourceStatus.ONLINE
            state.method = "http"
            # For Ollama, extract loaded models
            if "ollama" in res.id and isinstance(data, dict) and "models" in data:
                model_names = [m.get("name", "") for m in data.get("models", [])]
                state.details = {"models": model_names}
            return state

    # TCP fallback
    if res.port > 0 and await _check_tcp(res.host, res.port):
        state.status = ResourceStatus.ONLINE
        state.method = "tcp"
        return state

    state.status = ResourceStatus.OFFLINE
    state.method = "none"
    return state


# ---------------------------------------------------------------------------
# Monitor class
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """Periodically checks resource health and caches results.

    Thread safety: all state mutations happen on the event loop (single-threaded).
    Health checks run concurrently via asyncio.gather but cache updates are serial.
    """

    def __init__(self):
        self._resources = _build_resources()
        # Seed cache so get_all() returns the full list immediately
        # with "checking" status before the first health check completes.
        self._states: dict[str, ResourceState] = {
            res.id: ResourceState(
                id=res.id, name=res.name, category=res.category,
                status=ResourceStatus.CHECKING, method="",
            )
            for res in self._resources
        }
        self._task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None

    async def check_all(self) -> list[ResourceState]:
        """Async check of all resources concurrently. Updates cache."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        states = await asyncio.gather(
            *[_check_resource(res, self._http) for res in self._resources]
        )
        for state in states:
            self._states[state.id] = state
        return list(states)

    def get_all(self) -> list[ResourceState]:
        """Return cached states (sync, no I/O)."""
        return list(self._states.values())

    def get(self, resource_id: str) -> ResourceState | None:
        return self._states.get(resource_id)

    def is_available(self, resource_id: str) -> bool:
        state = self._states.get(resource_id)
        return state is not None and state.status == ResourceStatus.ONLINE

    async def start_background(self):
        """Start periodic health checks."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        self._task = asyncio.create_task(self._check_loop())

    async def stop_background(self):
        """Stop periodic health checks and close HTTP client."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _check_loop(self):
        """Background loop that checks resources every N seconds."""
        while True:
            try:
                await self.check_all()
            except Exception as e:
                logger.error("Health check error: %s", e)
            await asyncio.sleep(RESOURCE_CHECK_INTERVAL)
