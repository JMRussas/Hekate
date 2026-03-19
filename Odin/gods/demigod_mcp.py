"""Demigod MCP Server — spawn and manage jailed demigod runs via MCP.

This MCP server lets any client (Claude Code, Odin, other gods) spawn
demigod runs, check their status, cancel them, and retrieve results.

Features:
  - Concurrent run limiting (default 5, configurable via MAX_CONCURRENT)
  - Run cancellation via cancel_run tool
  - Optional event persistence to god_events table via Hades /exec
  - In-memory run tracking with auto-pruning

Tools:
  - list_roles: see available demigod roles
  - run_health_check: blocking health check run
  - spawn_demigod: background run, returns run ID
  - get_run: check status / get result
  - list_runs: list recent runs
  - cancel_run: abort a running demigod
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

from gods.demigod import (
    run_demigod, LLMConfig, DemigodResult, EventCallback,
    LLM_GATEWAY_URL, HADES_URL,
)
from gods.relay import EventRelay
from gods.roles.health_checker import (
    build_health_checker_role,
    build_batch_health_checker_role,
)

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
log = logging.getLogger("demigod-mcp")

mcp = FastMCP("demigod")

# Config from env (defaults, can be overridden by --config file)
GATEWAY = os.environ.get("LLM_GATEWAY_URL", LLM_GATEWAY_URL)
HADES = os.environ.get("HADES_URL", HADES_URL)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DEMIGODS", "5"))

# Event relay — configurable sinks for all god communication
_relay_config: dict = {}
_relay: EventRelay = EventRelay.default()

# Concurrency semaphore
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


def _load_config(path: str):
    """Load demigod MCP config from a JSON file."""
    global GATEWAY, HADES, MAX_CONCURRENT, _relay_config, _relay, _semaphore

    data = json.loads(open(path, encoding="utf-8").read())
    GATEWAY = data.get("gateway_url", GATEWAY)
    HADES = data.get("hades_url", HADES)
    MAX_CONCURRENT = data.get("max_concurrent", MAX_CONCURRENT)
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    relay_cfg = data.get("relay", {})
    if relay_cfg:
        _relay_config = relay_cfg
        _relay = EventRelay.from_config(relay_cfg)
        log.info("Event relay configured: %d sinks", len(_relay._sinks))
    else:
        _relay = EventRelay.default()

# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------

ROLE_BUILDERS = {
    "health_checker": build_health_checker_role,
    "batch_health_checker": build_batch_health_checker_role,
}

ROLE_DESCRIPTIONS = {
    "health_checker": (
        "Checks all Hekate NSSM services, inspects unhealthy ones, "
        "reads logs, restarts them, and verifies recovery. One service at a time."
    ),
    "batch_health_checker": (
        "Checks all services in one pass. Restarts every unhealthy service, "
        "then verifies all recovered. Handles multiple failures in a single run."
    ),
}

# ---------------------------------------------------------------------------
# Event relay integration
# ---------------------------------------------------------------------------

def _make_event_callback(run_id: str, role: str) -> EventCallback:
    """Create an event callback that routes through the relay."""
    source = f"demigod:{role}:{run_id}"
    return _relay.make_callback(source)


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    run_id: str
    role: str
    provider: str
    model: str | None
    status: str  # "running" | "completed" | "failed" | "cancelled"
    started_at: float
    finished_at: float | None = None
    result: DemigodResult | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


_runs: dict[str, RunRecord] = {}
_MAX_RUNS = 50


def _prune_runs():
    if len(_runs) > _MAX_RUNS:
        sorted_ids = sorted(_runs, key=lambda k: _runs[k].started_at)
        for rid in sorted_ids[: len(_runs) - _MAX_RUNS]:
            record = _runs.pop(rid)
            if record.task and not record.task.done():
                record.task.cancel()


def _active_count() -> int:
    return sum(1 for r in _runs.values() if r.status == "running")


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_roles() -> str:
    """List available demigod roles and their descriptions.

    Each role defines a jailed state machine — a specific job a demigod
    can do with constrained actions and context.
    """
    roles = []
    for name, desc in ROLE_DESCRIPTIONS.items():
        roles.append({"name": name, "description": desc})
    return json.dumps(roles, indent=2)


@mcp.tool()
async def run_health_check(
    provider: str = "gemini",
    model: str = "",
    batch: bool = False,
) -> str:
    """Run a health checker demigod (blocking — returns result when done).

    Checks all services, inspects unhealthy ones, restarts as needed,
    verifies recovery. Uses the LLM Gateway for decisions.

    Args:
        provider: LLM provider — "claude", "gemini", or "ollama". Default "gemini".
        model: Optional model override (provider-specific).
        batch: If true, use batch mode that handles all unhealthy services at once.
    """
    role_name = "batch_health_checker" if batch else "health_checker"
    return await _run_role_sync(role_name, provider, model or None)


@mcp.tool()
async def spawn_demigod(
    role: str,
    provider: str = "gemini",
    model: str = "",
    params: str = "{}",
) -> str:
    """Spawn a demigod run in the background. Returns a run ID immediately.

    Use get_run(run_id) to check progress and get the result.
    Respects the concurrent run limit (default 5).

    Args:
        role: Role name (use list_roles to see available roles).
        provider: LLM provider — "claude", "gemini", or "ollama".
        model: Optional model override.
        params: JSON string of initial parameters (e.g., '{"service_name": "HekateOrchestration"}').
    """
    if role not in ROLE_BUILDERS:
        return json.dumps({
            "error": f"Unknown role: {role}",
            "available": list(ROLE_BUILDERS.keys()),
        })

    try:
        initial_params = json.loads(params) if params else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"Invalid params JSON: {params}"})

    if _active_count() >= MAX_CONCURRENT:
        return json.dumps({
            "error": f"Concurrent limit reached ({MAX_CONCURRENT}). "
                     f"Wait for a running demigod to finish or cancel one.",
            "active_runs": _active_count(),
            "max_concurrent": MAX_CONCURRENT,
        })

    run_id = str(uuid.uuid4())[:8]
    llm = LLMConfig(provider=provider, model=model or None)
    role_obj = ROLE_BUILDERS[role]()

    record = RunRecord(
        run_id=run_id,
        role=role,
        provider=provider,
        model=model or None,
        status="running",
        started_at=time.time(),
    )

    async def _run():
        async with _semaphore:
            try:
                result = await run_demigod(
                    role_obj, llm,
                    hades_url=HADES,
                    gateway_url=GATEWAY,
                    initial_params=initial_params,
                    on_event=_make_event_callback(run_id, role),
                    cancel_event=record.cancel_event,
                )
                record.result = result
                if record.cancel_event.is_set():
                    record.status = "cancelled"
                elif result.success:
                    record.status = "completed"
                else:
                    record.status = "failed"
            except Exception as e:
                record.status = "failed"
                record.result = DemigodResult(
                    role=role, success=False, steps=0,
                    duration_ms=0, final_state="error",
                    error=str(e),
                )
            finally:
                record.finished_at = time.time()

    record.task = asyncio.create_task(_run(), name=f"demigod-{run_id}")
    _runs[run_id] = record
    _prune_runs()

    return json.dumps({
        "run_id": run_id,
        "role": role,
        "provider": provider,
        "status": "running",
        "active_runs": _active_count(),
    }, indent=2)


@mcp.tool()
def get_run(run_id: str) -> str:
    """Get the status and result of a demigod run.

    Args:
        run_id: The run ID returned by spawn_demigod.
    """
    record = _runs.get(run_id)
    if not record:
        return json.dumps({"error": f"Unknown run: {run_id}"})

    out: dict[str, Any] = {
        "run_id": record.run_id,
        "role": record.role,
        "provider": record.provider,
        "model": record.model,
        "status": record.status,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }

    if record.result:
        out["result"] = {
            "success": record.result.success,
            "steps": record.result.steps,
            "duration_ms": round(record.result.duration_ms),
            "final_state": record.result.final_state,
            "error": record.result.error,
            "history": record.result.history,
        }

    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def list_runs(limit: int = 10) -> str:
    """List recent demigod runs with their status.

    Args:
        limit: Max number of runs to return. Default 10.
    """
    sorted_runs = sorted(
        _runs.values(), key=lambda r: r.started_at, reverse=True
    )[:limit]

    out = []
    for r in sorted_runs:
        entry: dict[str, Any] = {
            "run_id": r.run_id,
            "role": r.role,
            "provider": r.provider,
            "status": r.status,
            "started_at": r.started_at,
        }
        if r.result:
            entry["success"] = r.result.success
            entry["steps"] = r.result.steps
            entry["duration_ms"] = round(r.result.duration_ms)
            entry["error"] = r.result.error
        out.append(entry)

    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def cancel_run(run_id: str) -> str:
    """Cancel a running demigod. The run will stop at the next step boundary.

    Args:
        run_id: The run ID to cancel.
    """
    record = _runs.get(run_id)
    if not record:
        return json.dumps({"error": f"Unknown run: {run_id}"})

    if record.status != "running":
        return json.dumps({
            "error": f"Run {run_id} is not running (status={record.status})",
        })

    record.cancel_event.set()
    return json.dumps({
        "run_id": run_id,
        "status": "cancelling",
        "message": "Cancellation requested. Run will stop at next step boundary.",
    })


@mcp.tool()
def status() -> str:
    """Get demigod server status: active runs, capacity, config."""
    active = [r for r in _runs.values() if r.status == "running"]
    return json.dumps({
        "active_runs": len(active),
        "max_concurrent": MAX_CONCURRENT,
        "total_runs": len(_runs),
        "persist_events": PERSIST_EVENTS,
        "gateway_url": GATEWAY,
        "hades_url": HADES,
        "available_roles": list(ROLE_BUILDERS.keys()),
        "running": [
            {"run_id": r.run_id, "role": r.role, "started_at": r.started_at}
            for r in active
        ],
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run_role_sync(
    role_name: str,
    provider: str,
    model: str | None,
    initial_params: dict | None = None,
) -> str:
    """Run a role synchronously and return the result as JSON."""
    builder = ROLE_BUILDERS.get(role_name)
    if not builder:
        return json.dumps({"error": f"Unknown role: {role_name}"})

    if _active_count() >= MAX_CONCURRENT:
        return json.dumps({
            "error": f"Concurrent limit reached ({MAX_CONCURRENT})",
        })

    run_id = str(uuid.uuid4())[:8]
    role_obj = builder()
    llm = LLMConfig(provider=provider, model=model)

    async with _semaphore:
        result = await run_demigod(
            role_obj, llm,
            hades_url=HADES,
            gateway_url=GATEWAY,
            initial_params=initial_params,
            on_event=_make_event_callback(run_id, role_name),
        )

    return json.dumps({
        "run_id": run_id,
        "success": result.success,
        "steps": result.steps,
        "duration_ms": round(result.duration_ms),
        "final_state": result.final_state,
        "error": result.error,
        "history": result.history,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="Demigod MCP Server")
    _parser.add_argument(
        "--config", "-c",
        help="Path to config JSON (gateway_url, hades_url, max_concurrent, relay)",
    )
    _args = _parser.parse_args()
    if _args.config:
        _load_config(_args.config)
    mcp.run()
