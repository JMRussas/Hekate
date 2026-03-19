"""Health Checker role — a demigod that checks and fixes service health.

This is the demigod's entire world. It can:
  1. Check all services
  2. Inspect a specific unhealthy service (pick which one)
  3. Read its logs
  4. Restart it
  5. Verify it came back
  6. Report done

It cannot: touch the filesystem, run arbitrary commands, access databases,
or do anything outside these actions. It's in a prison.
"""

from __future__ import annotations

import json
import httpx

from gods.demigod import Role, State, Action, LLMConfig, RunContext


# ---------------------------------------------------------------------------
# Context gatherers — fetch structured state from Hades, present to model
# ---------------------------------------------------------------------------

async def gather_overview(ctx: RunContext) -> dict:
    """Get all services with status. The demigod sees a clean summary."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{ctx.hades_url}/services")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return {
            "error": f"Cannot reach Hades at {ctx.hades_url}",
            "instruction": "Hades admin service is down. Pick needs_human.",
        }
    except Exception as e:
        return {"error": f"Failed to query services: {e}"}

    summary = {}
    unhealthy = []
    for name, info in data.items():
        status = info.get("nssm_status", "unknown")
        health = info.get("health_ok")
        summary[name] = {"status": status, "healthy": health}
        if status != "SERVICE_RUNNING" or health is False:
            unhealthy.append(name)

    result: dict = {
        "services": summary,
        "total": len(summary),
    }

    if unhealthy:
        result["unhealthy"] = unhealthy
        # Give the model the exact names to pick from
        result["instruction"] = (
            f"These services need attention: {', '.join(unhealthy)}. "
            f"Pick inspect:<exact_service_name> using one of these names."
        )
    else:
        result["unhealthy"] = "none — all services healthy"

    return result


async def gather_inspect(ctx: RunContext) -> dict:
    """Show detail for the selected service + its last action result."""
    svc = ctx.params.get("service_name", "unknown")
    out: dict = {"selected_service": svc}

    # Include the result from whatever got us here (status check, logs, etc.)
    if ctx.last_result:
        if "error" in ctx.last_result:
            out["last_error"] = ctx.last_result["error"]
            if "detail" in ctx.last_result:
                out["error_detail"] = ctx.last_result["detail"]
        else:
            out["service_info"] = ctx.last_result

    return out


async def gather_verify(ctx: RunContext) -> dict:
    """After a restart, re-check everything so the model can verify."""
    overview = await gather_overview(ctx)
    svc = ctx.params.get("service_name", "unknown")
    overview["restarted_service"] = svc
    # Include the restart result
    if ctx.last_result:
        overview["restart_result"] = ctx.last_result
    return overview


# ---------------------------------------------------------------------------
# Role definition — the state machine
# ---------------------------------------------------------------------------

def build_health_checker_role() -> Role:
    return Role(
        name="health_checker",
        description="Checks service health and restarts unhealthy services",
        system_prompt=(
            "You are a server health checker. Your ONLY job is to check "
            "service health and fix problems by restarting services. "
            "You have no other capabilities. Process: "
            "check all → identify problems → inspect → fix → verify."
        ),
        initial_state="check_all",
        max_steps=15,
        states={
            # ----- check all services -----
            "check_all": State(
                name="check_all",
                prompt=(
                    "Review the service status below. If any are unhealthy "
                    "or stopped, inspect one. If all healthy, you're done."
                ),
                gather=gather_overview,
                actions=[
                    Action(
                        name="inspect",
                        description="Inspect a specific service — provide its name",
                        param="service_name",
                        mcp_call={
                            "server": "hades",
                            "tool": "service_status",
                            "args_template": {"name": "{service_name}"},
                        },
                        next_state="inspect",
                    ),
                    Action(
                        name="all_healthy",
                        description="All services are running and healthy. Done.",
                        terminal=True,
                    ),
                ],
            ),

            # ----- inspect a specific service -----
            "inspect": State(
                name="inspect",
                prompt=(
                    "You're inspecting a specific service. You can read its "
                    "logs, restart it, go back, or escalate to a human."
                ),
                gather=gather_inspect,
                actions=[
                    Action(
                        name="tail_logs",
                        description="Read the last 50 log lines for this service",
                        mcp_call={
                            "server": "hades",
                            "tool": "tail_logs",
                            "args_template": {
                                "service": "{service_name}",
                                "lines": 50,
                            },
                        },
                        next_state="inspect",  # stay — can read logs multiple times
                    ),
                    Action(
                        name="restart",
                        description="Restart this service and wait for health check",
                        mcp_call={
                            "server": "hades",
                            "tool": "restart_service",
                            "args_template": {"name": "{service_name}"},
                        },
                        next_state="verify",
                    ),
                    Action(
                        name="check_all",
                        description="Go back and check all services again",
                        next_state="check_all",
                    ),
                    Action(
                        name="needs_human",
                        description="This problem needs human intervention — escalate",
                        terminal=True,
                    ),
                ],
            ),

            # ----- verify after restart -----
            "verify": State(
                name="verify",
                prompt=(
                    "A service was just restarted. Check whether it recovered. "
                    "If still unhealthy, inspect it again. If all good, done."
                ),
                gather=gather_verify,
                actions=[
                    Action(
                        name="recovered",
                        description="Service recovered. Check for more problems.",
                        next_state="check_all",
                    ),
                    Action(
                        name="still_unhealthy",
                        description="Service is still unhealthy. Inspect it again.",
                        next_state="inspect",
                    ),
                    Action(
                        name="all_healthy",
                        description="All services are now healthy. Done.",
                        terminal=True,
                    ),
                    Action(
                        name="needs_human",
                        description="Cannot fix automatically — escalate to human.",
                        terminal=True,
                    ),
                ],
            ),
        },
    )


def default_llm() -> LLMConfig:
    """Default LLM config — uses Gemini via the gateway (fast, cheap)."""
    return LLMConfig(
        provider="gemini",
        model=None,  # gateway default
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Batch health checker — handles all unhealthy services in one pass
# ---------------------------------------------------------------------------

async def gather_batch_overview(ctx: RunContext) -> dict:
    """Get all services. Track which ones still need fixing."""
    overview = await gather_overview(ctx)
    if "error" in overview:
        return overview

    # Track which services we've already fixed
    fixed = ctx.params.get("_fixed_services", "")
    fixed_set = set(fixed.split(",")) if fixed else set()

    unhealthy = overview.get("unhealthy", [])
    if isinstance(unhealthy, list):
        remaining = [s for s in unhealthy if s not in fixed_set]
        overview["remaining_to_fix"] = remaining if remaining else "none"
        overview["already_fixed"] = sorted(fixed_set) if fixed_set else "none"
        if remaining:
            overview["instruction"] = (
                f"Fix these services one at a time: {', '.join(remaining)}. "
                f"Pick fix_next:{remaining[0]} to start with the first one."
            )
    return overview


async def gather_batch_fixing(ctx: RunContext) -> dict:
    """Context while fixing a specific service in batch mode."""
    svc = ctx.params.get("service_name", "unknown")
    out: dict = {"fixing_service": svc}
    if ctx.last_result:
        if "error" in ctx.last_result:
            out["last_error"] = ctx.last_result["error"]
        else:
            out["action_result"] = ctx.last_result
    return out


def build_batch_health_checker_role() -> Role:
    """Batch health checker — fixes all unhealthy services in one run."""
    return Role(
        name="batch_health_checker",
        description="Checks and fixes all unhealthy services in a single pass",
        system_prompt=(
            "You are a server health checker running in batch mode. "
            "Your job is to fix ALL unhealthy services, one at a time. "
            "For each: restart it, verify it recovered, then move to the next. "
            "Only stop when all services are healthy or you've tried them all."
        ),
        initial_state="scan",
        max_steps=30,  # more steps since we handle multiple services
        states={
            # ----- scan: check what needs fixing -----
            "scan": State(
                name="scan",
                prompt=(
                    "Review the services below. If any remain unhealthy, "
                    "pick fix_next with the service name. If all are healthy, done."
                ),
                gather=gather_batch_overview,
                actions=[
                    Action(
                        name="fix_next",
                        description="Restart the next unhealthy service — provide its name",
                        param="service_name",
                        mcp_call={
                            "server": "hades",
                            "tool": "restart_service",
                            "args_template": {"name": "{service_name}"},
                        },
                        next_state="fixing",
                    ),
                    Action(
                        name="all_healthy",
                        description="All services are now healthy. Done.",
                        terminal=True,
                    ),
                    Action(
                        name="needs_human",
                        description="Remaining problems need human intervention.",
                        terminal=True,
                    ),
                ],
            ),

            # ----- fixing: we just restarted, check if it worked -----
            "fixing": State(
                name="fixing",
                prompt=(
                    "You just restarted a service. Check if it recovered. "
                    "If yes, mark it fixed and scan for more. If not, "
                    "read logs or escalate."
                ),
                gather=gather_batch_fixing,
                actions=[
                    Action(
                        name="fixed",
                        description="Service recovered. Move to the next one.",
                        handler=_mark_fixed,
                        next_state="scan",
                    ),
                    Action(
                        name="tail_logs",
                        description="Read the last 50 log lines to diagnose",
                        mcp_call={
                            "server": "hades",
                            "tool": "tail_logs",
                            "args_template": {
                                "service": "{service_name}",
                                "lines": 50,
                            },
                        },
                        next_state="fixing",
                    ),
                    Action(
                        name="retry",
                        description="Try restarting this service again",
                        mcp_call={
                            "server": "hades",
                            "tool": "restart_service",
                            "args_template": {"name": "{service_name}"},
                        },
                        next_state="fixing",
                    ),
                    Action(
                        name="skip",
                        description="Can't fix this one — skip to the next service",
                        handler=_mark_fixed,  # still mark as "attempted"
                        next_state="scan",
                    ),
                    Action(
                        name="needs_human",
                        description="Cannot fix any more services. Escalate.",
                        terminal=True,
                    ),
                ],
            ),
        },
    )


async def _mark_fixed(ctx: RunContext) -> dict:
    """Mark the current service as fixed/attempted in the batch tracker."""
    svc = ctx.params.get("service_name", "")
    if svc:
        existing = ctx.params.get("_fixed_services", "")
        fixed_set = set(existing.split(",")) if existing else set()
        fixed_set.discard("")
        fixed_set.add(svc)
        ctx.params["_fixed_services"] = ",".join(sorted(fixed_set))
    return {"marked": svc, "total_fixed": len(ctx.params.get("_fixed_services", "").split(","))}
