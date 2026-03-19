"""Handler registration — wires all handlers into a pipeline.

Gap 7: Single function that registers every handler with its event type.
Gap 8: Cursor persistence via god_registry table.
"""

from __future__ import annotations

from gods.pipeline import Pipeline

from gods.handlers.athena import athena_plan, athena_reassess
from gods.handlers.odin import (
    odin_start, odin_tick, odin_dispatch,
    odin_lifecycle, odin_handle_diagnosis,
)
from gods.handlers.hermes import hermes_execute
from gods.handlers.mimir import (
    mimir_verify, mimir_review,
    mimir_handle_review_rejection, mimir_handle_task_rejection,
)
from gods.handlers.hephaestus import hephaestus_stage
from gods.handlers.tyche import tyche_record_spend


def register_all_handlers(pipeline: Pipeline) -> None:
    """Register all god handlers with their event type subscriptions."""

    # Athena — planning
    pipeline.register("project_created", athena_plan)
    pipeline.register("wave_complete", athena_reassess)

    # Odin — orchestration
    pipeline.register("project_planned", odin_start)
    pipeline.register("tick", odin_dispatch, filter=lambda e: "project_id" in e.payload)
    pipeline.register("tick", odin_tick, filter=lambda e: "project_id" not in e.payload)
    pipeline.register("task_verified", odin_lifecycle)
    pipeline.register("task_diagnosis", odin_handle_diagnosis)

    # Hermes — execution
    pipeline.register("dispatch_command", hermes_execute)

    # Mimir — verification + review
    pipeline.register("worker_event", mimir_verify)
    pipeline.register("task_verified", mimir_review)
    pipeline.register("review_rejected", mimir_handle_review_rejection)
    pipeline.register("task_rejected", mimir_handle_task_rejection)

    # Hephaestus — git
    pipeline.register("task_verified", hephaestus_stage)

    # Tyche — budget
    pipeline.register("worker_event", tyche_record_spend)
