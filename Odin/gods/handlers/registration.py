"""Handler registration — wires all handlers into a pipeline.

register_all_handlers() creates the HermesRunner and binds all god
handlers to their event types. Returns the runner for shutdown access.
"""

from __future__ import annotations

from gods.pipeline import Pipeline
from gods.handlers.hermes_async import HermesRunner

from gods.handlers.athena import athena_plan, athena_reassess
from gods.handlers.odin import (
    odin_start, odin_tick, odin_dispatch,
    odin_lifecycle, odin_handle_diagnosis,
)
from gods.handlers.mimir import (
    mimir_verify, mimir_review,
    mimir_handle_review_rejection, mimir_handle_task_rejection,
)
from gods.handlers.hephaestus import hephaestus_stage
from gods.handlers.tyche import tyche_record_spend


def register_all_handlers(
    pipeline: Pipeline,
    *,
    max_concurrent: int = 4,
    heartbeat_interval: float = 30.0,
) -> HermesRunner:
    """Register all god handlers with their event type subscriptions.

    Returns the HermesRunner instance (needed for shutdown/cancel).
    """
    # Create async hermes runner
    hermes = HermesRunner(
        db=pipeline.db,
        max_concurrent=max_concurrent,
        heartbeat_interval=heartbeat_interval,
    )

    # Athena — planning
    pipeline.register("project_created", athena_plan)
    pipeline.register("wave_complete", athena_reassess)

    # Odin — orchestration
    pipeline.register("project_planned", odin_start)
    pipeline.register("tick", odin_dispatch, filter=lambda e: "project_id" in e.payload)
    pipeline.register("tick", odin_tick, filter=lambda e: "project_id" not in e.payload)
    pipeline.register("task_verified", odin_lifecycle)
    pipeline.register("task_diagnosis", odin_handle_diagnosis)

    # Hermes — async execution (non-blocking)
    pipeline.register("dispatch_command", hermes.handle_dispatch)

    # Mimir — verification + review
    pipeline.register("worker_event", mimir_verify)
    pipeline.register("task_verified", mimir_review)
    pipeline.register("review_rejected", mimir_handle_review_rejection)
    pipeline.register("task_rejected", mimir_handle_task_rejection)

    # Hephaestus — git
    pipeline.register("task_verified", hephaestus_stage)

    # Tyche — budget
    pipeline.register("worker_event", tyche_record_spend)

    return hermes
