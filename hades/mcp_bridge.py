#!/usr/bin/env python3
"""
Hades MCP Bridge — exposes the Hades admin API as MCP tools.

Thin stdio MCP server that proxies to the Hades HTTP API on port 5201.
Lets any Claude Code chat use service management, deployment, command
execution, and log tailing as native MCP tools.
"""

import json
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

HADES_URL = os.environ.get("HADES_URL", "http://localhost:5201")
_PORT = int(os.environ.get("MCP_PORT", "0"))

mcp = FastMCP("hades", port=_PORT) if _PORT else FastMCP("hades")
log = logging.getLogger("hades-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "hades", "port": 5211})


def _client() -> httpx.Client:
    return httpx.Client(base_url=HADES_URL, timeout=httpx.Timeout(630.0))


# ---------------------------------------------------------------------------
# Service Management
# ---------------------------------------------------------------------------

@mcp.tool()
def list_services() -> str:
    """List all Hekate NSSM services with their status and health."""
    with _client() as c:
        resp = c.get("/services")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def service_status(name: str) -> str:
    """Get status and health of a specific NSSM service.

    Args:
        name: Service name (e.g. HekateOrchestration, HekateContextStore,
              HekateServer, HekatePythonWorker, Ollama, ComfyUI)
    """
    with _client() as c:
        resp = c.get(f"/services/{name}")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def restart_service(name: str) -> str:
    """Restart a specific NSSM service with health check.

    Args:
        name: Service name to restart
    """
    with _client() as c:
        resp = c.post(f"/services/{name}/restart")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def stop_service(name: str) -> str:
    """Stop a specific NSSM service.

    Args:
        name: Service name to stop
    """
    with _client() as c:
        resp = c.post(f"/services/{name}/stop")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def start_service(name: str) -> str:
    """Start a specific NSSM service with health check.

    Args:
        name: Service name to start
    """
    with _client() as c:
        resp = c.post(f"/services/{name}/start")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def restart_core() -> str:
    """Restart core services (Orchestration + Context Store) with health checks."""
    with _client() as c:
        resp = c.post("/restart-core")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def restart_all() -> str:
    """Restart all managed Hekate services (those with managed=true in config)."""
    with _client() as c:
        resp = c.post("/restart-all")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def create_service(name: str, app: str, app_args: str = "", app_dir: str = "",
                   port: int = 0, health: str = "", group: str = "custom",
                   managed: bool = True) -> str:
    """Provision a new NSSM service and register it in the config.

    Args:
        name: Service name (must start with 'Hekate')
        app: Path to the executable
        app_args: Command-line arguments for the executable
        app_dir: Working directory (defaults to exe's parent dir)
        port: Port the service listens on (0 = none)
        health: Health check URL (empty = none)
        group: Service group tag (core, mcp, custom, external)
        managed: Whether deploy should manage this service
    """
    payload = {
        "name": name, "app": app, "app_args": app_args,
        "app_dir": app_dir, "group": group, "managed": managed,
    }
    if port:
        payload["port"] = port
    if health:
        payload["health"] = health

    with _client() as c:
        resp = c.post("/services", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def remove_service(name: str) -> str:
    """Remove an NSSM service and unregister it from the config.

    Args:
        name: Service name to remove (will stop it first if running)
    """
    with _client() as c:
        resp = c.delete(f"/services/{name}", params={"confirm": "true"})
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def sync_check() -> str:
    """Compare the service config against actual NSSM state. Reports drift."""
    with _client() as c:
        resp = c.get("/services/sync-check")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Command Execution
# ---------------------------------------------------------------------------

@mcp.tool()
def exec(command: str, cwd: str = "", timeout: int = 120, shell: str = "bash") -> str:
    """Execute a shell command on the Hekate host machine.

    Supports bash, cmd, powershell. Use this for npm, python, git, dotnet,
    or any command available on PATH.

    Args:
        command: The command to execute (e.g. "npm run build", "git status",
                "python -m pytest tests/")
        cwd: Working directory. Defaults to the source repo root.
        timeout: Max seconds to wait (1-600). Default 120.
        shell: Shell to use — "bash", "cmd", or "powershell". Default "bash".
    """
    payload = {"command": command, "shell": shell, "timeout": min(timeout, 600)}
    if cwd:
        payload["cwd"] = cwd

    with _client() as c:
        resp = c.post("/exec", json=payload, timeout=httpx.Timeout(timeout + 30.0))
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

@mcp.tool()
def deploy(skip_frontend: bool = False) -> str:
    """Run the full deploy script (source repo → C:\\Hekate).

    Stops services, publishes binaries, syncs DB, builds frontend,
    syntax checks, starts services, health checks.

    Args:
        skip_frontend: Skip the frontend npm build step.
    """
    with _client() as c:
        resp = c.post("/deploy", json={"skip_frontend": skip_frontend},
                       timeout=httpx.Timeout(330.0))
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Log Tailing
# ---------------------------------------------------------------------------

@mcp.tool()
def tail_logs(service: str, lines: int = 50) -> str:
    """Tail the stdout/stderr log files for an NSSM service.

    Args:
        service: Service name (e.g. HekateOrchestration)
        lines: Number of lines to return from the end. Default 50.
    """
    with _client() as c:
        resp = c.get(f"/logs/{service}", params={"lines": lines})
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Cache & System
# ---------------------------------------------------------------------------

@mcp.tool()
def clear_pycache(path: str = "") -> str:
    """Recursively delete __pycache__ directories.

    Args:
        path: Root path to clean. Defaults to C:\\Hekate\\orchestration.
    """
    payload = {}
    if path:
        payload["path"] = path

    with _client() as c:
        resp = c.post("/clear-pycache", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
def system_info() -> str:
    """Get basic system information (hostname, platform, Python version, paths)."""
    with _client() as c:
        resp = c.get("/info")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _PORT:
        mcp.run(transport="sse")
    else:
        mcp.run()
