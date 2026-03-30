"""Workflow primitives — DECISION, FORK, JOIN.

Handlers:
  - odin_decide: wave_assessed → evaluate condition → pick branch → cancel others
  - odin_fork: task_fork_requested → create N sub-tasks from template + output

JOIN is handled in odin_lifecycle (threshold-aware unblock query).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from gods.pipeline import Event, Emit, idem_key
from gods import safe_json

logger = logging.getLogger("gods.handlers.odin_workflow")


# ---------------------------------------------------------------------------
# Rule engine — deterministic decision evaluation
# ---------------------------------------------------------------------------

def _evaluate_rules(
    rules: list[dict],
    task_outcomes: list[dict],
    fallback_branch: str = "default",
) -> tuple[str, str]:
    """Evaluate rule-based conditions against task outcomes.

    Returns (chosen_branch, reason).
    """
    completed = [t for t in task_outcomes if t.get("status") == "completed"]
    failed = [t for t in task_outcomes if t.get("status") == "failed"]

    for rule in rules:
        condition = rule.get("condition", "")
        branch = rule.get("branch", fallback_branch)

        if condition == "all_passed" and len(failed) == 0 and len(completed) > 0:
            return branch, f"All {len(completed)} tasks passed"

        if condition == "any_failed" and len(failed) > 0:
            return branch, f"{len(failed)} task(s) failed"

        if condition == "all_failed" and len(completed) == 0 and len(failed) > 0:
            return branch, f"All {len(failed)} tasks failed"

        if condition.startswith("output_contains:"):
            search_term = condition.split(":", 1)[1]
            for t in completed:
                output = t.get("output_text", "") or ""
                if search_term in output:
                    return branch, f"Output contains '{search_term}'"

        if condition.startswith("min_completed:"):
            threshold = int(condition.split(":", 1)[1])
            if len(completed) >= threshold:
                return branch, f"{len(completed)} >= {threshold} tasks completed"

    return fallback_branch, "No rules matched, using fallback"


# ---------------------------------------------------------------------------
# LLM decision evaluation
# ---------------------------------------------------------------------------

async def _evaluate_llm(
    spec: dict,
    task_outcomes: list[dict],
    project_id: str,
) -> tuple[str, str]:
    """Evaluate decision via LLM gateway. Returns (chosen_branch, reason)."""
    from gods.config import GATEWAY_URL
    import httpx

    branches = spec.get("branches", {})
    branch_names = list(branches.keys())
    prompt_template = spec.get("prompt_template", "")

    # Build outcomes summary
    outcome_lines = []
    for t in task_outcomes:
        status = t.get("status", "unknown")
        title = t.get("title", "?")
        output = (t.get("output_text", "") or "")[:500]
        outcome_lines.append(f"- [{status}] {title}: {output}")

    user_message = f"""Task outcomes from the completed wave:

{chr(10).join(outcome_lines)}

{prompt_template}

Available branches: {', '.join(branch_names)}

Choose one branch. Respond with JSON: {{"branch": "<name>", "reason": "<one sentence>"}}"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{GATEWAY_URL}/v1/chat", json={
                "provider": "claude",
                "system_prompt": "You are a workflow decision engine. Pick the best branch based on task outcomes.",
                "user_message": user_message,
            })
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "") or data.get("content", "")

            # Parse JSON from response
            result = safe_json.loads_dict(text)
            branch = result.get("branch", "")
            reason = result.get("reason", "LLM decision")

            if branch in branch_names:
                return branch, reason
            # LLM returned unknown branch — use fallback
            logger.warning("LLM returned unknown branch '%s', using fallback", branch)
    except Exception as e:
        logger.error("LLM decision evaluation failed: %s", e)

    return spec.get("fallback_branch", "default"), "LLM evaluation failed, using fallback"


# ---------------------------------------------------------------------------
# Branch cancellation
# ---------------------------------------------------------------------------

async def _cancel_branch_tasks(db, project_id: str, branch_id: str) -> int:
    """Cancel all tasks in a branch. Returns count of cancelled tasks."""
    now = time.time()
    result = await db.execute_write(
        "UPDATE tasks SET status = $1, updated_at = $2 "
        "WHERE project_id = $3 AND branch_id = $4 AND status NOT IN ($5, $6, $7)",
        ("cancelled", now, project_id, branch_id, "completed", "failed", "cancelled"),
    )
    # Extract count from result if available
    count = 0
    if result and isinstance(result, str):
        try:
            count = int(result.strip().split()[-1])
        except (ValueError, IndexError):
            pass
    return count


# ---------------------------------------------------------------------------
# odin_decide — handles wave_assessed events
# ---------------------------------------------------------------------------

async def odin_decide(event: Event, db) -> list[Emit] | None:
    """Evaluate a decision point after wave assessment.

    If no workflow_edge exists for this wave, emits project_tick (backward compat).
    If a decision edge exists, evaluates it and activates the chosen branch.
    """
    project_id = event.payload.get("project_id")
    wave = event.payload.get("wave")

    if not project_id:
        return None

    # Check for a decision edge on this wave
    edge = await db.fetchone(
        "SELECT id, spec_json, status FROM workflow_edges "
        "WHERE project_id = $1 AND source_wave = $2 AND edge_type = $3 AND status = $4",
        (project_id, wave, "decision", "pending"),
    )

    if not edge:
        # No decision — just emit project_tick (backward compat)
        return [Emit("project_tick", {"project_id": project_id}, source="odin_decide")]

    edge_id = edge["id"]
    spec = safe_json.loads_dict(edge.get("spec_json", "{}"))
    strategy = spec.get("strategy", "rule")
    branches = spec.get("branches", {})
    fallback_branch = spec.get("fallback_branch", "default")

    # Load task outcomes from the completed wave
    task_outcomes = await db.fetchall(
        "SELECT id, title, status, output_text FROM tasks "
        "WHERE project_id = $1 AND wave = $2",
        (project_id, wave),
    )
    outcomes = [dict(t) if not isinstance(t, dict) else t for t in task_outcomes]

    # Evaluate decision
    if strategy == "rule":
        rules = spec.get("rules", [])
        chosen_branch, reason = _evaluate_rules(rules, outcomes, fallback_branch)
    elif strategy == "llm":
        chosen_branch, reason = await _evaluate_llm(spec, outcomes, project_id)
    elif strategy == "llm_with_rules":
        # Try rules first, fall back to LLM
        rules = spec.get("rules", [])
        chosen_branch, reason = _evaluate_rules(rules, outcomes, "")
        if not chosen_branch:
            chosen_branch, reason = await _evaluate_llm(spec, outcomes, project_id)
    else:
        chosen_branch = fallback_branch
        reason = f"Unknown strategy '{strategy}', using fallback"

    logger.info("Decision %s: chose branch '%s' — %s", edge_id[:8], chosen_branch, reason)

    # Record decision in odin_decisions
    decision_id = uuid.uuid4().hex[:12]
    now = time.time()
    try:
        await db.execute_write(
            "INSERT INTO odin_decisions "
            "(decision_id, project_id, decision_type, params_json, confidence, "
            "reasoning, action_taken, details_json, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            (decision_id, project_id, "workflow_decision",
             json.dumps(spec), 1.0, reason, chosen_branch,
             json.dumps({"edge_id": edge_id, "wave": wave}), now),
        )
    except Exception as e:
        logger.warning("Failed to record decision: %s", e)

    # Cancel tasks in unchosen branches
    cancelled_total = 0
    for branch_name in branches:
        if branch_name != chosen_branch:
            count = await _cancel_branch_tasks(db, project_id, branch_name)
            cancelled_total += count

    # Mark edge as evaluated
    await db.execute_write(
        "UPDATE workflow_edges SET status = $1, result_json = $2, evaluated_at = $3 "
        "WHERE id = $4",
        ("evaluated", json.dumps({"branch": chosen_branch, "reason": reason}), now, edge_id),
    )

    logger.info("Decision %s: cancelled %d tasks in unchosen branches", edge_id[:8], cancelled_total)

    return [
        Emit("workflow_decision_made", {
            "project_id": project_id,
            "edge_id": edge_id,
            "branch": chosen_branch,
            "reason": reason,
            "cancelled_count": cancelled_total,
        }, source="odin_decide",
           idempotency_key=idem_key("decision", edge_id),
        ),
        Emit("project_tick", {"project_id": project_id}, source="odin_decide"),
    ]


# ---------------------------------------------------------------------------
# odin_fork — handles task_fork_requested events
# ---------------------------------------------------------------------------

MAX_FAN_OUT = 20


def make_odin_fork_handler(pipeline):
    """Factory: create odin_fork bound to pipeline for atomic task creation."""

    async def odin_fork(event: Event, db) -> list[Emit] | None:
        """Create N sub-tasks from a fork specification.

        Reads the fork spec from the workflow_edge or event payload.
        Creates sub-tasks in the same wave as the source task.
        Adds deps: each sub-task depends on source, join task depends on all sub-tasks.
        """
        source_task_id = event.payload.get("source_task_id")
        project_id = event.payload.get("project_id")
        items = event.payload.get("items", [])
        fork_group_id = event.payload.get("fork_group_id") or uuid.uuid4().hex[:12]

        if not source_task_id or not project_id or not items:
            return [Emit("odin_error", {
                "error": "Fork requires source_task_id, project_id, and items",
            }, source="odin_fork")]

        # Load source task for wave and context
        source = await db.fetchone(
            "SELECT wave, plan_id, context_json FROM tasks WHERE id = $1",
            (source_task_id,),
        )
        if not source:
            return [Emit("odin_error", {
                "error": f"Source task {source_task_id} not found",
            }, source="odin_fork")]

        wave = source.get("wave", 0)
        plan_id = source.get("plan_id", "")

        # Load fork spec from workflow_edge if exists
        edge = await db.fetchone(
            "SELECT id, spec_json FROM workflow_edges "
            "WHERE source_task_id = $1 AND edge_type = $2 AND status = $3",
            (source_task_id, "fork", "pending"),
        )
        spec = {}
        edge_id = None
        if edge:
            edge_id = edge["id"]
            spec = safe_json.loads_dict(edge.get("spec_json", "{}"))

        template = spec.get("template_task", event.payload.get("template", {}))
        max_fan_out = spec.get("max_fan_out", MAX_FAN_OUT)
        join_task_id = spec.get("join_task_id") or event.payload.get("join_task_id")

        # Cap items
        if len(items) > max_fan_out:
            logger.warning("Fork capped: %d items → %d (max_fan_out)", len(items), max_fan_out)
            items = items[:max_fan_out]

        now = time.time()
        created_ids = []

        for i, item in enumerate(items):
            task_id = uuid.uuid4().hex[:12]
            item_str = item if isinstance(item, str) else json.dumps(item)

            title = (template.get("title_template", "Fork task {i}: {item}")
                     .replace("{i}", str(i))
                     .replace("{item}", item_str[:80]))
            description = (template.get("description_template", "Process: {item}")
                          .replace("{item}", item_str))
            task_type = template.get("task_type", "code")

            context = {
                "fork_source_task_id": source_task_id,
                "fork_item": item,
                "fork_index": i,
            }

            await db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, "
                "task_type, wave, status, fork_group_id, context_json, "
                "created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                (task_id, project_id, plan_id, title, description,
                 task_type, wave, "pending", fork_group_id,
                 json.dumps(context), now, now),
            )

            # Sub-task depends on source
            await db.execute_write(
                "INSERT INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
                (task_id, source_task_id),
            )

            created_ids.append(task_id)

        # If join task exists, add deps: join depends on each forked task
        if join_task_id:
            for cid in created_ids:
                await db.execute_write(
                    "INSERT INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
                    (join_task_id, cid),
                )

        # Mark edge evaluated
        if edge_id:
            await db.execute_write(
                "UPDATE workflow_edges SET status = $1, result_json = $2, evaluated_at = $3 "
                "WHERE id = $4",
                ("evaluated", json.dumps({
                    "created_count": len(created_ids),
                    "fork_group_id": fork_group_id,
                    "task_ids": created_ids,
                }), now, edge_id),
            )

        logger.info("Fork %s: created %d sub-tasks (group=%s)",
                     source_task_id[:8], len(created_ids), fork_group_id[:8])

        return [
            Emit("tasks_forked", {
                "project_id": project_id,
                "source_task_id": source_task_id,
                "fork_group_id": fork_group_id,
                "count": len(created_ids),
                "task_ids": created_ids,
            }, source="odin_fork",
               idempotency_key=idem_key("fork", source_task_id, fork_group_id),
            ),
            Emit("project_tick", {"project_id": project_id}, source="odin_fork"),
        ]

    return odin_fork
