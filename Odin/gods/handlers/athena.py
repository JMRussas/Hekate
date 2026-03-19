"""Athena handler — planning.

Takes project_created events, generates plans via PlannerService,
reviews them, and emits project_planned when ready.

Internal functions (_generate_plan, _review_plan, _reassess_wave) are
module-level so tests can patch them without reaching into the planner
or LLM internals.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.athena")

# Max times to regenerate a plan based on review feedback
MAX_REVIEW_ITERATIONS = 2


# ---------------------------------------------------------------------------
# Internal functions — patched in tests, wired to real code in production
# ---------------------------------------------------------------------------

async def _generate_plan(
    project_id: str,
    db,
    *,
    comments: list[dict] | None = None,
    previous_plan: dict | None = None,
) -> dict:
    """Generate a plan. In production, wraps PlannerService.generate().

    Returns: {"plan_id": str, "plan": dict, ...}
    """
    # Default implementation — import and call the real planner.
    # Tests patch this to avoid LLM calls.
    from backend.services.planner import PlannerService
    from backend.services.budget import BudgetManager

    budget = BudgetManager(db)
    planner = PlannerService(db=db, budget=budget)
    result = await planner.generate(
        project_id,
        comments=comments,
        previous_plan=previous_plan,
    )

    # Decompose plan into executable task rows
    plan_id = result.get("plan_id")
    if plan_id:
        from backend.services.decomposer import DecomposerService
        decomposer = DecomposerService(db=db)
        decomp = await decomposer.decompose(project_id, plan_id)
        result["task_count"] = decomp.get("tasks_created", decomp.get("task_count", 0))
        logger.info("Athena: decomposed plan %s → %d tasks", plan_id[:8], result["task_count"])

    return result


async def _review_plan(
    plan: dict,
    requirements: str,
    project_name: str = "",
    **kwargs,
):
    """Review a plan. In production, wraps review.review_plan().

    Returns: PlanReview(has_gaps, confidence, gaps, feedback, ...)
    """
    from gods.review import review_plan
    from backend.services.llm_router import call_llm
    return await review_plan(
        plan, requirements, project_name,
        call_llm=call_llm,
        **kwargs,
    )


async def _reassess_wave(
    project_id: str,
    wave: int,
    db,
) -> Any:
    """Reassess a completed wave. In production, wraps PlannerService.evaluate_wave_reassessment()."""
    from backend.services.planner import PlannerService
    from backend.services.budget import BudgetManager
    from backend.models.schemas import WaveReassessmentContext

    budget = BudgetManager(db)
    planner = PlannerService(db=db, budget=budget)

    # Build context from DB
    tasks = await db.fetchall(
        "SELECT title, status, output_text FROM tasks "
        "WHERE project_id = $1 AND wave = $2",
        (project_id, wave),
    )
    task_outcomes = []
    for t in tasks:
        title = t["title"] if isinstance(t, dict) else t[0]
        status = t["status"] if isinstance(t, dict) else t[1]
        output = t["output_text"] if isinstance(t, dict) else t[2]
        task_outcomes.append({
            "title": title,
            "status": status,
            "output_summary": (output or "")[:500],
        })

    context = WaveReassessmentContext(
        project_id=project_id,
        wave_number=wave,
        task_outcomes=task_outcomes,
    )
    return await planner.evaluate_wave_reassessment(context)


# ---------------------------------------------------------------------------
# athena_plan — main handler
# ---------------------------------------------------------------------------

async def athena_plan(event: Event, db) -> list[Emit] | None:
    """Handle project_created → generate plan → review → emit project_planned.

    Registered as: pipeline.register("project_created", athena_plan, gate=check_plan_created)
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        return [Emit("planning_failed", {"error": "No project_id in event"}, source="athena")]

    # Check for gate feedback (retry after gate failure)
    gate_feedback = event.payload.get("_gate_feedback")
    comments = None
    if gate_feedback:
        comments = [{"author": "gate", "content": gate_feedback}]
        logger.info("Athena: regenerating with gate feedback: %s", gate_feedback[:80])

    try:
        # Load project requirements for review
        project_row = await db.fetchone(
            "SELECT name, requirements FROM projects WHERE id = $1",
            (project_id,),
        )
        if not project_row:
            return [Emit("planning_failed", {
                "project_id": project_id,
                "error": "Project not found",
            }, source="athena")]

        project_name = project_row["name"] if isinstance(project_row, dict) else project_row[0]
        requirements = project_row["requirements"] if isinstance(project_row, dict) else project_row[1]

        # Generate → Review → Regenerate loop
        previous_plan = None
        result = None

        for iteration in range(MAX_REVIEW_ITERATIONS + 1):
            logger.info(
                "Athena: generating plan for %s (iteration %d/%d)",
                project_id[:8], iteration + 1, MAX_REVIEW_ITERATIONS + 1,
            )

            result = await _generate_plan(
                project_id, db,
                comments=comments,
                previous_plan=previous_plan,
            )

            plan = result.get("plan", {})
            plan_id = result.get("plan_id", "")

            # Review the plan
            try:
                review = await _review_plan(
                    plan, requirements, project_name,
                )
            except Exception as e:
                logger.warning("Review failed, proceeding without: %s", e)
                review = type("FakeReview", (), {
                    "has_gaps": False, "confidence": 0.5,
                    "gaps": [], "feedback": "",
                })()

            # If review passes or we're at max iterations, emit
            if not review.has_gaps or review.confidence >= 0.7 or iteration >= MAX_REVIEW_ITERATIONS:
                return [Emit("project_planned", {
                    "project_id": project_id,
                    "plan_id": plan_id,
                    "review": {
                        "has_gaps": review.has_gaps,
                        "confidence": review.confidence,
                        "gaps": review.gaps,
                    },
                }, source="athena")]

            # Review found gaps — regenerate with feedback
            logger.info(
                "Athena: review found %d gaps (confidence=%.2f), regenerating",
                len(review.gaps), review.confidence,
            )
            comments = [{"author": "plan_reviewer", "content": review.feedback}]
            previous_plan = plan

        # Shouldn't reach here, but emit best effort
        return [Emit("project_planned", {
            "project_id": project_id,
            "plan_id": result.get("plan_id", "") if result else "",
            "review": {"has_gaps": True, "confidence": 0.0, "gaps": ["Max iterations reached"]},
        }, source="athena")]

    except Exception as e:
        logger.error("Athena: planning failed for %s: %s", project_id, e)
        return [Emit("planning_failed", {
            "project_id": project_id,
            "error": str(e),
        }, source="athena", severity="error")]


# ---------------------------------------------------------------------------
# athena_reassess — wave reassessment handler
# ---------------------------------------------------------------------------

async def athena_reassess(event: Event, db) -> list[Emit] | None:
    """Handle wave_complete → reassess → emit wave_assessed.

    Registered as: pipeline.register("wave_complete", athena_reassess)
    """
    project_id = event.payload.get("project_id")
    wave = event.payload.get("wave", 0)

    try:
        result = await _reassess_wave(project_id, wave, db)

        return [Emit("wave_assessed", {
            "project_id": project_id,
            "wave": wave,
            "outcome": result.outcome,
            "rationale": result.rationale,
            "suggested_changes": getattr(result, "suggested_changes", []),
        }, source="athena")]

    except Exception as e:
        logger.error("Athena: reassessment failed for %s wave %d: %s", project_id, wave, e)
        return [Emit("wave_assessed", {
            "project_id": project_id,
            "wave": wave,
            "outcome": "escalate_to_human",
            "rationale": f"Reassessment failed: {e}",
        }, source="athena")]
