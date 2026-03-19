"""God MCP Server — exposes god capabilities as jailed MCP tools.

This is the MCP interface that demigods (and other consumers) talk to.
Tool visibility is controlled by a config file passed at spawn time —
not env vars, not hardcoded. The config defines the role, which tools
are allowed, and where events should be relayed.

Usage:
    # Admin — all tools visible
    python god_mcp.py

    # Jailed to a role defined in a config file
    python god_mcp.py --config path/to/god_spawn.json

Config file format (god_spawn.json):
    {
        "role": "health_checker",
        "allowed_tools": ["list_services", "service_status", ...],
        "hades_url": "http://localhost:5201",
        "relay_url": "http://localhost:5201/events"
    }

If no --config is provided, all tools are registered (admin mode).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
log = logging.getLogger("god-mcp")


# ---------------------------------------------------------------------------
# Spawn config — loaded from file, defines the jail
# ---------------------------------------------------------------------------

class GodConfig:
    """Configuration for this god MCP server instance."""

    def __init__(
        self,
        role: str = "admin",
        allowed_tools: set[str] | None = None,
        hades_url: str = "http://localhost:5201",
        relay_url: str | None = None,
    ):
        self.role = role
        self.allowed_tools = allowed_tools  # None = all tools
        self.hades_url = hades_url
        self.relay_url = relay_url

    @classmethod
    def from_file(cls, path: str | Path) -> GodConfig:
        """Load config from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = data.get("allowed_tools")
        return cls(
            role=data.get("role", "admin"),
            allowed_tools=set(allowed) if allowed else None,
            hades_url=data.get("hades_url", "http://localhost:5201"),
            relay_url=data.get("relay_url"),
        )

    @classmethod
    def from_god_json(cls, god_json_path: str | Path, role_name: str) -> GodConfig:
        """Load config from a god.json manifest for a specific role."""
        data = json.loads(Path(god_json_path).read_text(encoding="utf-8"))
        config = data.get("config", {})
        relay_url = config.get("relay_url")
        hades_url = config.get("hades_url", "http://localhost:5201")

        allowed = None
        for role_def in data.get("roles", []):
            if role_def.get("name") == role_name:
                allowed = set(role_def.get("allowed_god_tools", []))
                break

        return cls(
            role=role_name,
            allowed_tools=allowed,
            hades_url=hades_url,
            relay_url=relay_url,
        )

    def is_allowed(self, tool_name: str) -> bool:
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools


# ---------------------------------------------------------------------------
# Hades client
# ---------------------------------------------------------------------------

_config: GodConfig = GodConfig()  # default admin, overwritten by main()


def _hades() -> httpx.Client:
    return httpx.Client(
        base_url=_config.hades_url,
        timeout=httpx.Timeout(630.0),
    )


# ---------------------------------------------------------------------------
# Tool implementations — plain functions, registered selectively
# ---------------------------------------------------------------------------

def _list_services() -> str:
    """List all Hekate services with their NSSM status and health.

    Returns a JSON object keyed by service name with status, health,
    and port for each service.
    """
    with _hades() as c:
        resp = c.get("/services")
        resp.raise_for_status()
        raw = resp.json()
    clean = {}
    for name, info in raw.items():
        clean[name] = {
            "status": info.get("nssm_status", "unknown"),
            "healthy": info.get("health_ok"),
            "port": info.get("port"),
            "group": info.get("group"),
        }
    return json.dumps(clean, indent=2)


def _service_status(name: str) -> str:
    """Get detailed status of a specific NSSM service.

    Args:
        name: Service name (e.g., HekateOrchestration, HekateContextStore)
    """
    with _hades() as c:
        resp = c.get(f"/services/{name}")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _restart_service(name: str) -> str:
    """Restart a specific service with health check verification.

    Args:
        name: Service name to restart
    """
    with _hades() as c:
        resp = c.post(f"/services/{name}/restart")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _start_service(name: str) -> str:
    """Start a stopped service with health check.

    Args:
        name: Service name to start
    """
    with _hades() as c:
        resp = c.post(f"/services/{name}/start")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _stop_service(name: str) -> str:
    """Stop a running service.

    Args:
        name: Service name to stop
    """
    with _hades() as c:
        resp = c.post(f"/services/{name}/stop")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _restart_core() -> str:
    """Restart core services (Orchestration + Context Store) with health checks."""
    with _hades() as c:
        resp = c.post("/restart-core")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _restart_all() -> str:
    """Restart all managed services (those with managed=true in config)."""
    with _hades() as c:
        resp = c.post("/restart-all")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _tail_logs(service: str, lines: int = 50) -> str:
    """Read the last N lines of a service's stdout/stderr logs.

    Args:
        service: Service name (e.g., HekateOrchestration)
        lines: Number of lines from the end. Default 50.
    """
    with _hades() as c:
        resp = c.get(f"/logs/{service}", params={"lines": lines})
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _sync_check() -> str:
    """Compare service config against actual NSSM state. Reports drift."""
    with _hades() as c:
        resp = c.get("/services/sync-check")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _system_info() -> str:
    """Get host system information (hostname, platform, Python, paths)."""
    with _hades() as c:
        resp = c.get("/info")
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _deploy(skip_frontend: bool = False) -> str:
    """Run full deploy: stop services → publish → copy → build → start.

    Args:
        skip_frontend: Skip the frontend npm build step. Default false.
    """
    with _hades() as c:
        resp = c.post(
            "/deploy",
            json={"skip_frontend": skip_frontend},
            timeout=httpx.Timeout(330.0),
        )
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


def _clear_pycache(path: str = "") -> str:
    """Recursively delete __pycache__ directories.

    Args:
        path: Root path to clean. Defaults to orchestration directory.
    """
    payload = {"path": path} if path else {}
    with _hades() as c:
        resp = c.post("/clear-pycache", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

ALL_TOOLS: dict[str, Any] = {
    "list_services": _list_services,
    "service_status": _service_status,
    "restart_service": _restart_service,
    "start_service": _start_service,
    "stop_service": _stop_service,
    "restart_core": _restart_core,
    "restart_all": _restart_all,
    "tail_logs": _tail_logs,
    "sync_check": _sync_check,
    "system_info": _system_info,
    "deploy": _deploy,
    "clear_pycache": _clear_pycache,
}

# Predefined role → tool sets (used when spawning from god.json)
ROLE_TOOLS: dict[str, set[str]] = {
    "health_checker": {
        "list_services", "service_status", "restart_service",
        "start_service", "stop_service", "tail_logs",
    },
    "deployer": {
        "list_services", "service_status", "deploy",
        "restart_all", "restart_core", "clear_pycache", "sync_check",
    },
    "observer": {
        "list_services", "service_status", "tail_logs",
        "sync_check", "system_info",
    },
}


def get_tools_for_role(role: str | None) -> list[str]:
    """Return the tool names visible to a given role."""
    if not role or role == "admin":
        return sorted(ALL_TOOLS.keys())
    allowed = ROLE_TOOLS.get(role, set())
    return sorted(allowed)


def build_mcp(config: GodConfig) -> FastMCP:
    """Build a FastMCP server with only the tools allowed by config."""
    server = FastMCP(f"god-{config.role}")
    registered = []
    for tool_name, tool_fn in ALL_TOOLS.items():
        if config.is_allowed(tool_name):
            server.tool()(tool_fn)
            registered.append(tool_name)

    log.info(
        "God MCP (role=%s): %d/%d tools: %s",
        config.role, len(registered), len(ALL_TOOLS), registered,
    )
    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _config

    parser = argparse.ArgumentParser(description="God MCP Server")
    parser.add_argument(
        "--config", "-c",
        help="Path to spawn config JSON (defines role, allowed tools, URLs)",
    )
    parser.add_argument(
        "--god-json",
        help="Path to god.json manifest (used with --role)",
    )
    parser.add_argument(
        "--role", "-r",
        default="admin",
        help="Role name (used with --god-json, or standalone for predefined roles)",
    )
    args = parser.parse_args()

    if args.config:
        _config = GodConfig.from_file(args.config)
    elif args.god_json:
        _config = GodConfig.from_god_json(args.god_json, args.role)
    elif args.role != "admin":
        # Use predefined role tools
        tools = ROLE_TOOLS.get(args.role)
        _config = GodConfig(
            role=args.role,
            allowed_tools=tools,
            hades_url=os.environ.get("HADES_URL", "http://localhost:5201"),
            relay_url=os.environ.get("EVENT_RELAY_URL"),
        )
    else:
        _config = GodConfig(
            hades_url=os.environ.get("HADES_URL", "http://localhost:5201"),
            relay_url=os.environ.get("EVENT_RELAY_URL"),
        )

    server = build_mcp(_config)
    server.run()


if __name__ == "__main__":
    main()
