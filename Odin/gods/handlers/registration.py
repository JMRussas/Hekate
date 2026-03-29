"""Handler registration — wires all handlers into a pipeline.

register_all_handlers() creates the HermesRunner and MimirRunner,
binds all god handlers to their event types. Returns both runners
for shutdown access.
"""

from __future__ import annotations

from gods.pipeline import Pipeline
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
from gods.handlers.mimir import (
    mimir_review,
    mimir_handle_review_rejection, mimir_handle_task_rejection,
)
from gods.handlers.hephaestus import hephaestus_stage
from gods.handlers.tyche import tyche_record_spend
from gods.handlers.context_bridge import (
    context_bridge_plan,
    context_bridge_task_verified,
    context_bridge_project_complete,
)


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
    pipeline.register("project_created", athena_plan_leveled)
    # New parallel node tree planner: handles projects with use_node_tree_planner=True
    # athena_l0 self-filters based on config_json — both can be registered safely
    pipeline.register("project_created", athena_l0)
    # Node tree handlers (fan-out deepening + completion propagation)
    pipeline.register("plan_node_created", athena_deepen)
    pipeline.register("plan_node_complete", athena_bubble_up)
    pipeline.register("plan_node_executable", athena_materialize)
    pipeline.register("wave_complete", athena_reassess_standalone)

    # Odin — orchestration
    pipeline.register("project_planned", odin_start)
    pipeline.register("project_tick", odin_dispatch)
    pipeline.register("tick", odin_tick)
    pipeline.register("task_verified", odin_lifecycle)
    pipeline.register("task_diagnosis", make_odin_handle_diagnosis(pipeline))

    # Hermes — async execution (non-blocking)
    pipeline.register("dispatch_command", hermes.handle_dispatch)

    # Mimir — verification (non-blocking) + review
    pipeline.register("worker_event", mimir.handle_verify)
    pipeline.register("task_verified", mimir_review)
    pipeline.register("review_rejected", mimir_handle_review_rejection)
    pipeline.register("task_rejected", mimir_handle_task_rejection)

    # Hephaestus — git
    pipeline.register("task_verified", hephaestus_stage)

    # Tyche — budget
    pipeline.register("worker_event", tyche_record_spend)

    # Context bridge — sync pipeline state to context store graph
    pipeline.register("project_planned", context_bridge_plan)
    pipeline.register("task_verified", context_bridge_task_verified)
    pipeline.register("project_complete", context_bridge_project_complete)

    return hermes, mimir
