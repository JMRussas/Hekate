"""Handler registration — wires all handlers into a pipeline.

register_all_handlers() creates the HermesRunner and MimirRunner,
binds all god handlers to their event types. Returns both runners
for shutdown access.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from gods.pipeline import Pipeline, Event, Emit
from gods.handlers.hermes_async import HermesRunner
from gods.handlers.mimir import MimirRunner
from gods.providers.base import ProviderRegistry

from gods.handlers.athena_leveled import athena_plan_leveled, athena_reassess_standalone
from gods.handlers.athena_l0 import athena_l0
from gods.handlers.athena_deepen import athena_deepen
from gods.handlers.athena_complete import athena_bubble_up, athena_materialize
from gods.handlers.odin import (
    odin_start, odin_tick, odin_dispatch,
    odin_lifecycle, make_odin_handle_diagnosis,
)
from gods.handlers.odin_workflow import odin_decide, make_odin_fork_handler
from gods.handlers.mimir import (
    mimir_review,
    mimir_handle_review_rejection, mimir_handle_task_rejection,
)
from gods.handlers.hephaestus import hephaestus_stage, hephaestus_complete
from gods.handlers.tyche import tyche_record_spend, tyche_retry_rate_limited, make_tyche_budget_gate, make_tyche_rate_gate
from gods.rate_limit import ProviderRateLimiter
from gods.config import parse_rate_limit_config
from gods.gates import (
    check_plan_created,
    check_plan_reviewed,
    check_task_claimed,
    check_code_written,
    check_files_staged,
    check_pr_created,
    compose_gates,
)
from gods.handlers.context_bridge import (
    context_bridge_plan,
    context_bridge_task_verified,
    context_bridge_project_complete,
)
from gods.flags import flag_gate

_reg_logger = logging.getLogger("gods.handlers.registration")

# Shared rate limiter — single instance for the process
_rate_limit_defaults = {
    "claude_code": {"rate": 10, "window_seconds": 60, "burst": 12},
    "ollama": {"rate": 20, "window_seconds": 60, "burst": 25},
}
_rate_limits = {
    provider: parse_rate_limit_config(
        os.environ.get(f"HEKATE_RATE_LIMIT_{provider.upper()}"),
        defaults,
    )
    for provider, defaults in _rate_limit_defaults.items()
}
_rate_limiter = ProviderRateLimiter(_rate_limits)


async def _create_checkpoint_on_review(event: Event, db) -> list[Emit] | None:
    """Create a checkpoint row when a task needs human review."""
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    reason = event.payload.get("reason", "Needs human review")

    if not project_id:
        return None

    # Look up task for summary context
    summary = reason
    if task_id:
        task_row = await db.fetchone("SELECT title, retry_count, max_retries FROM tasks WHERE id = $1", (task_id,))
        if task_row:
            retries = task_row.get("retry_count", 0)
            max_r = task_row.get("max_retries", 3)
            summary = f"Task '{task_row['title']}' needs review after {retries}/{max_r} attempts: {reason}"

    # Build attempts from relay events for this task
    attempts = []
    if task_id:
        attempt_rows = await db.fetchall(
            "SELECT payload, created_at FROM god_relay_events "
            "WHERE event_type = 'worker_event' AND payload LIKE $1 "
            "ORDER BY created_at DESC LIMIT 5",
            (f'%{task_id}%',),
        )
        for ar in (attempt_rows or []):
            try:
                p = json.loads(ar["payload"]) if isinstance(ar["payload"], str) else ar["payload"]
                attempts.append({"error": p.get("error", ""), "timestamp": ar["created_at"]})
            except (json.JSONDecodeError, KeyError):
                pass

    checkpoint_id = str(uuid.uuid4())
    now = time.time()
    schema = json.dumps({"type": "object", "properties": {
        "action": {"type": "string", "enum": ["approve", "reject"]},
        "reason": {"type": "string", "maxLength": 10000},
    }, "required": ["action"]})

    await db.execute_write(
        "INSERT INTO checkpoints "
        "(id, project_id, task_id, checkpoint_type, summary, attempts_json, question, schema_json, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        (checkpoint_id, project_id, task_id, "retry_exhausted",
         summary, json.dumps(attempts),
         "How should this task be handled? (retry / skip / fail)",
         schema, now),
    )
    _reg_logger.info("Checkpoint %s created for task %s", checkpoint_id[:8], (task_id or "?")[:8])
    return None


def register_all_handlers(
    pipeline: Pipeline,
    *,
    max_concurrent: int = 4,
    max_concurrent_verifications: int = 4,
    heartbeat_interval: float = 30.0,
) -> tuple[HermesRunner, MimirRunner]:
    """Register all god handlers with their event type subscriptions.

    Returns (HermesRunner, MimirRunner) instances (needed for shutdown/cancel).
    """
    # Create provider registry with default providers (claude_code, gemini_cli)
    registry = ProviderRegistry()
    registry.register_defaults()

    # Create async hermes runner with provider registry
    hermes = HermesRunner(
        db=pipeline.db,
        registry=registry,
        max_concurrent=max_concurrent,
        heartbeat_interval=heartbeat_interval,
    )

    # Create async mimir runner for non-blocking verification
    mimir = MimirRunner(
        db=pipeline.db,
        max_concurrent=max_concurrent_verifications,
    )

    # Athena — planning
    # Old planner: handles projects without use_node_tree_planner flag (default)
    pipeline.register("project_created", athena_plan_leveled,
                       filter=flag_gate("athena_leveled"),
                       gate=check_plan_created, max_retries=2)
    # New parallel node tree planner: handles projects with use_node_tree_planner=True
    # athena_l0 self-filters based on config_json — both can be registered safely
    pipeline.register("project_created", athena_l0,
                       filter=flag_gate("athena_l0"),
                       gate=check_plan_created, max_retries=2)
    # Node tree handlers (fan-out deepening + completion propagation)
    pipeline.register("plan_node_created", athena_deepen,
                       filter=flag_gate("athena_deepen"))
    pipeline.register("plan_node_complete", athena_bubble_up,
                       filter=flag_gate("athena_bubble_up"))
    pipeline.register("plan_node_executable", athena_materialize,
                       filter=flag_gate("athena_materialize"))
    pipeline.register("wave_complete", athena_reassess_standalone,
                       filter=flag_gate("athena_reassess"))

    # Odin — orchestration
    pipeline.register("project_planned", odin_start,
                       filter=flag_gate("odin_dispatch"),
                       gate=check_plan_reviewed, max_retries=0)

    # Budget + rate gates on dispatch
    budget_config = {
        "daily_limit_usd": float(os.environ.get("HEKATE_DAILY_BUDGET_USD", "5.0")),
        "per_project_limit_usd": float(os.environ.get("HEKATE_PROJECT_BUDGET_USD", "inf")),
    }
    dispatch_gate = compose_gates(
        make_tyche_budget_gate(budget_config),
        make_tyche_rate_gate(_rate_limiter),
    )
    pipeline.register("project_tick", odin_dispatch,
                       filter=flag_gate("odin_dispatch"),
                       gate=dispatch_gate, max_retries=0)
    pipeline.register("tick", odin_tick,
                       filter=flag_gate("odin_dispatch"))
    pipeline.register("task_verified", odin_lifecycle,
                       filter=flag_gate("odin_lifecycle"))
    pipeline.register("task_diagnosis", make_odin_handle_diagnosis(pipeline),
                       filter=flag_gate("odin_lifecycle"))

    # Workflow primitives — DECISION, FORK
    pipeline.register("wave_assessed", odin_decide,
                       filter=flag_gate("odin_workflow"))
    pipeline.register("task_fork_requested", make_odin_fork_handler(pipeline),
                       filter=flag_gate("odin_workflow"))

    # Hermes — async execution (non-blocking)
    pipeline.register("dispatch_command", hermes.handle_dispatch,
                       filter=flag_gate("hermes_async"),
                       gate=check_task_claimed, max_retries=1)

    # Mimir — verification (non-blocking) + review
    pipeline.register("worker_event", mimir.handle_verify,
                       filter=flag_gate("mimir_review"),
                       gate=check_code_written, max_retries=0)
    pipeline.register("task_verified", mimir_review,
                       filter=flag_gate("mimir_review"))
    pipeline.register("review_rejected", mimir_handle_review_rejection,
                       filter=flag_gate("mimir_rejection"))
    pipeline.register("task_rejected", mimir_handle_task_rejection,
                       filter=flag_gate("mimir_rejection"))

    # Hephaestus — git
    pipeline.register("task_verified", hephaestus_stage,
                       filter=flag_gate("hephaestus"),
                       gate=check_files_staged, max_retries=1)
    pipeline.register("project_complete", hephaestus_complete,
                       filter=flag_gate("hephaestus"),
                       gate=check_pr_created, max_retries=1)

    # Tyche — budget + rate-limit retry
    pipeline.register("worker_event", tyche_record_spend,
                       filter=flag_gate("tyche_spend"))
    pipeline.register("gate_failed", tyche_retry_rate_limited,
                       filter=flag_gate("tyche_rate_limit"))

    # Context bridge — sync pipeline state to context store graph
    pipeline.register("project_planned", context_bridge_plan,
                       filter=flag_gate("context_bridge"))
    pipeline.register("task_verified", context_bridge_task_verified,
                       filter=flag_gate("context_bridge"))
    pipeline.register("project_complete", context_bridge_project_complete,
                       filter=flag_gate("context_bridge"))

    # Checkpoints — create DB rows when tasks need human review
    pipeline.register("needs_human_review", _create_checkpoint_on_review)

    return hermes, mimir
