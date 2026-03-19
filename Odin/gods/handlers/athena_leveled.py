"""Athena leveled planning handler.

Two-model architecture:
  Model A (generator): one continuous conversation, context accumulates L1→L2→L3
  Model B (reviewer): fresh prompt each time, no shared context, unbiased critique

Flow:
  project_created
    → L1 generate (Model A)
    → rule check L1
    → L2 deepen (Model A, same conversation)
    → rule check L2
    → L3 deepen (Model A, same conversation)
    → rule check L3
    → thorough review (Model B, fresh)
    → rule check review
    → if rejected: feedback → Model A fixes → rule check → review again
    → TDD phase (if enabled): generate test specs
    → project_planned
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from gods.pipeline import Event, Emit
from gods.plan_levels import (
    PlanLevel,
    TaskSpec,
    PlanConfig,
    RuleResult,
    validate_plan,
    validate_task_at_level,
    suggest_target_level,
)

logger = logging.getLogger("gods.handlers.athena_leveled")

# Max retries when rule check fails at a given level
MAX_RULE_RETRIES = 2

# Max review cycles (Model B rejects → Model A fixes)
MAX_REVIEW_CYCLES = 2


# ---------------------------------------------------------------------------
# Internal functions — patched in tests
# ---------------------------------------------------------------------------

async def _generate_l1(
    project_id: str,
    requirements: str,
    project_name: str,
    db,
    **kwargs,
) -> dict:
    """Generate L1 plan (Model A, first turn).

    Returns: {"plan_id": str, "tasks": [...], "conversation_id": str}
    The conversation_id is used to continue the same conversation in deepen calls.
    """
    # Default: import and call the real planner
    # NOTE: Do NOT decompose here — decomposition happens once after final review
    from backend.services.planner import PlannerService
    from backend.services.budget import BudgetManager

    budget = BudgetManager(db)
    planner = PlannerService(db=db, budget=budget)
    result = await planner.generate(project_id, **kwargs)

    return result


async def _deepen_plan(
    project_id: str,
    current_plan: dict,
    target_level: PlanLevel,
    conversation_id: str | None,
    requirements: str,
    db,
    *,
    review_feedback: str | None = None,
    **kwargs,
) -> dict:
    """Deepen plan to next level (Model A, continued conversation).

    Takes the current plan and adds detail for the target level.
    Uses conversation_id to continue Model A's conversation.

    Returns: {"plan_id": str, "tasks": [...], "conversation_id": str}
    """
    from backend.services.planner import PlannerService
    from backend.services.budget import BudgetManager

    budget = BudgetManager(db)
    planner = PlannerService(db=db, budget=budget)

    comments = []
    if review_feedback:
        comments.append({"author": "plan_reviewer", "content": review_feedback})

    result = await planner.generate(
        project_id,
        comments=comments or None,
        previous_plan=current_plan,
        conversation_id=conversation_id,
        **kwargs,
    )
    return result


async def _thorough_review(
    plan: dict,
    requirements: str,
    project_name: str = "",
    level: PlanLevel = PlanLevel.L3,
    **kwargs,
) -> dict:
    """Thorough review by Model B (fresh prompt, no shared context).

    Returns: {"approved": bool, "confidence": float, "feedback": str}
    """
    from gods.review import review_plan
    from backend.services.llm_router import call_llm

    review = await review_plan(
        plan, requirements, project_name,
        call_llm=call_llm,
        provider="gemini" if kwargs.get("generator_provider") != "gemini" else "claude",
        **{k: v for k, v in kwargs.items() if k != "generator_provider"},
    )

    return {
        "approved": not review.has_gaps or review.confidence >= 0.7,
        "confidence": review.confidence,
        "feedback": review.feedback,
        "gaps": review.gaps,
    }


async def _generate_tdd_tests(
    plan: dict,
    requirements: str,
    project_name: str = "",
    **kwargs,
) -> dict:
    """Generate TDD test specs for the plan.

    Returns: {"test_specs": [{"task_id": str, "test_file": str, "test_cases": [...]}]}
    """
    from backend.services.llm_router import call_llm

    tasks = plan.get("tasks", [])
    code_tasks = [t for t in tasks if t.get("task_type") == "code"]

    test_specs = []
    for task in code_tasks:
        test_specs.append({
            "task_id": task["id"],
            "test_file": f"tests/test_{task['id']}.py",
            "test_cases": [
                f"test_{task['title'].lower().replace(' ', '_')}_happy_path",
                f"test_{task['title'].lower().replace(' ', '_')}_edge_cases",
            ],
        })

    return {"test_specs": test_specs}


# ---------------------------------------------------------------------------
# Helper: parse tasks from plan into TaskSpec objects
# ---------------------------------------------------------------------------

def _parse_tasks(plan_data: dict) -> list[TaskSpec]:
    """Convert raw plan/result dict into TaskSpec objects for validation.

    Handles multiple formats:
      - {"tasks": [...]} — flat task list
      - {"plan": {"phases": [{"tasks": [...]}]}} — planner result with phases
      - {"phases": [{"tasks": [...]}]} — plan JSON directly
    """
    raw_tasks = []

    # Try flat task list
    if plan_data.get("tasks"):
        raw_tasks = plan_data["tasks"]
    else:
        # Try nested phases (planner output)
        plan = plan_data.get("plan", plan_data)
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except (json.JSONDecodeError, TypeError):
                plan = {}
        for phase in plan.get("phases", []):
            raw_tasks.extend(phase.get("tasks", []))

    specs = []
    for i, t in enumerate(raw_tasks):
        specs.append(TaskSpec(
            id=t.get("id", f"task-{i}"),
            title=t.get("title", ""),
            task_type=t.get("task_type", "code"),
            wave=t.get("wave", i // 3),  # infer wave from position if not set
            description=t.get("description"),
            affected_files=t.get("affected_files"),
            depends_on=t.get("depends_on"),
            complexity=t.get("complexity"),
            implementation_notes=t.get("implementation_notes"),
            test_strategy=t.get("test_strategy"),
            edge_cases=t.get("edge_cases"),
            changes=t.get("changes"),
        ))
    return specs


# ---------------------------------------------------------------------------
# Helper: load project config
# ---------------------------------------------------------------------------

def _load_config(config_json: str | None) -> PlanConfig:
    """Parse project config_json into PlanConfig."""
    if not config_json:
        return PlanConfig()
    try:
        raw = json.loads(config_json)
        return PlanConfig(
            tdd=raw.get("tdd", True),
            narration=raw.get("narration", True),
            target_level=raw.get("target_level", "auto"),
            direct_write=raw.get("direct_write", True),
            max_concurrent=raw.get("max_concurrent", 2),
        )
    except (json.JSONDecodeError, TypeError):
        return PlanConfig()


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def athena_plan_leveled(event: Event, db) -> list[Emit] | None:
    """Handle project_created → leveled planning pipeline → project_planned.

    Model A generates and deepens (one conversation).
    Model B reviews (fresh prompt, no shared context).
    Rule checks between every level.
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        return [Emit("planning_failed", {"error": "No project_id"}, source="athena")]

    # Load project
    row = await db.fetchone(
        "SELECT name, requirements, status, config_json FROM projects WHERE id = $1",
        (project_id,),
    )
    if not row:
        return [Emit("planning_failed", {
            "project_id": project_id, "error": "Project not found",
        }, source="athena")]

    project_name = row["name"]
    requirements = row.get("requirements", "") or ""
    status = row.get("status", "draft")
    config = _load_config(row.get("config_json"))

    # Guard: skip if already beyond draft
    if status not in ("draft",):
        return []

    # Lock to planning
    await db.execute_write(
        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
        ("planning", time.time(), project_id),
    )

    emits: list[Emit] = []

    def narrate(msg: str):
        if config.narration:
            emits.append(Emit("narration", {
                "project_id": project_id,
                "text": msg,
            }, source="athena"))

    try:
        # Check which tooling services are actually running
        from gods.tooling import check_tooling_availability
        try:
            tooling = await check_tooling_availability()
            tooling_flags = {
                "has_roslyn": tooling.has_roslyn,
                "has_jedi": tooling.has_jedi,
                "has_ts_compiler": tooling.has_ts_compiler,
            }
        except Exception as e:
            logger.warning("Tooling availability check failed: %s", e)
            tooling_flags = {"has_roslyn": False, "has_jedi": False, "has_ts_compiler": False}

        narrate(f"Available tooling: Roslyn={tooling_flags['has_roslyn']}, "
                f"Jedi={tooling_flags['has_jedi']}, TS={tooling_flags['has_ts_compiler']}")

        # Determine target level — pass ALL tooling, model decides what's relevant
        if config.target_level == "auto":
            target = suggest_target_level(
                task_type="code", complexity="medium", **tooling_flags,
            )
        else:
            target = PlanLevel.from_str(config.target_level)

        narrate(f"Target planning depth: {target.name}")

        # ---------------------------------------------------------------
        # Step 1: L1 Generate (Model A, first turn)
        # ---------------------------------------------------------------
        narrate(f"Generating L1 plan for '{project_name}'...")

        current_plan = None
        conversation_id = None

        for retry in range(MAX_RULE_RETRIES + 1):
            l1_result = await _generate_l1(
                project_id, requirements, project_name, db,
            )
            current_plan = l1_result
            conversation_id = l1_result.get("conversation_id")

            # Rule check L1
            tasks = _parse_tasks(l1_result)
            rule_result = validate_plan(tasks, PlanLevel.L1, requirements=requirements)

            if rule_result.passed:
                narrate(f"L1 plan passed rule check ({len(tasks)} tasks)")
                break
            else:
                narrate(f"L1 rule check failed: {rule_result.reason}. Retrying...")
                if retry >= MAX_RULE_RETRIES:
                    await db.execute_write(
                        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                        ("failed", time.time(), project_id),
                    )
                    return emits + [Emit("planning_failed", {
                        "project_id": project_id,
                        "error": f"L1 rule validation failed after {MAX_RULE_RETRIES + 1} attempts: {rule_result.reason}",
                    }, source="athena")]

        # ---------------------------------------------------------------
        # Steps 2-3: Deepen to target level (Model A, same conversation)
        # ---------------------------------------------------------------
        current_level = PlanLevel.L1

        while current_level < target:
            next_level = current_level.next()
            if next_level is None:
                break

            narrate(f"Deepening plan to {next_level.name}...")

            level_reached = False
            for retry in range(MAX_RULE_RETRIES + 1):
                deepened = await _deepen_plan(
                    project_id, current_plan, next_level,
                    conversation_id, requirements, db,
                )
                current_plan = deepened
                conversation_id = deepened.get("conversation_id", conversation_id)

                # Rule check at new level
                tasks = _parse_tasks(deepened)
                rule_result = validate_plan(tasks, next_level, requirements=requirements)

                if rule_result.passed:
                    narrate(f"{next_level.name} plan passed rule check")
                    current_level = next_level
                    level_reached = True
                    break
                else:
                    narrate(f"{next_level.name} rule check failed: {rule_result.reason}")
                    if retry >= MAX_RULE_RETRIES:
                        narrate(f"Could not reach {next_level.name}, proceeding at {current_level.name}")
                        break

            if not level_reached:
                # Can't go deeper — stop the while loop
                break

        # ---------------------------------------------------------------
        # Step 4: Thorough review (Model B, fresh prompt)
        # ---------------------------------------------------------------
        for review_cycle in range(MAX_REVIEW_CYCLES + 1):
            narrate("Sending plan for thorough review (Model B)...")

            review = await _thorough_review(
                current_plan, requirements, project_name,
                level=current_level,
            )

            if review.get("approved"):
                narrate(f"Review approved (confidence: {review.get('confidence', 0):.0%})")
                break
            else:
                feedback = review.get("feedback", "")
                narrate(f"Review rejected: {feedback[:100]}...")

                if review_cycle >= MAX_REVIEW_CYCLES:
                    narrate("Max review cycles reached, proceeding with current plan")
                    break

                # Send feedback back to Model A
                narrate("Sending review feedback to generator...")
                fixed = await _deepen_plan(
                    project_id, current_plan, current_level,
                    conversation_id, requirements, db,
                    review_feedback=feedback,
                )
                current_plan = fixed
                conversation_id = fixed.get("conversation_id", conversation_id)

        # ---------------------------------------------------------------
        # Step 5: TDD phase (if enabled)
        # ---------------------------------------------------------------
        test_specs = None
        if config.tdd:
            narrate("Generating TDD test specs...")
            tdd_result = await _generate_tdd_tests(
                current_plan, requirements, project_name,
            )
            test_specs = tdd_result.get("test_specs")
            narrate(f"Generated {len(test_specs or [])} test specs")

        # ---------------------------------------------------------------
        # Step 6: Decompose plan into task rows (once, after all planning)
        # ---------------------------------------------------------------
        plan_id = current_plan.get("plan_id")
        if plan_id:
            narrate("Decomposing final plan into executable tasks...")
            try:
                from backend.services.decomposer import DecomposerService
                decomposer = DecomposerService(db=db)
                decomp = await decomposer.decompose(project_id, plan_id)
                task_count = decomp.get("tasks_created", decomp.get("task_count", 0))
                narrate(f"Created {task_count} tasks from plan")
            except Exception as e:
                logger.warning("Decomposition failed: %s", e)
                narrate(f"Decomposition failed: {e}")

        # ---------------------------------------------------------------
        # Done — emit project_planned
        # ---------------------------------------------------------------
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("planned", time.time(), project_id),
        )

        planned_payload = {
            "project_id": project_id,
            "plan_id": current_plan.get("plan_id", ""),
            "level": current_level.name,
            "review": review,
        }
        if test_specs:
            planned_payload["test_specs"] = test_specs

        emits.append(Emit("project_planned", planned_payload, source="athena"))
        return emits

    except Exception as e:
        logger.error("Athena leveled: planning failed for %s: %s", project_id, e)
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("failed", time.time(), project_id),
        )
        return emits + [Emit("planning_failed", {
            "project_id": project_id,
            "error": str(e),
        }, source="athena", severity="error")]
