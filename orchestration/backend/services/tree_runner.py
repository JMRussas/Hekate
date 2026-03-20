#  Orchestration Engine - Tree Runner
#
#  Deterministic step-tree executor. The tree owns control flow;
#  the model owns judgment at each leaf node.
#
#  For Claude Code CLI: maintains a single conversation session across
#  steps via --resume. The model keeps its full context — files read,
#  code written, decisions made. Each step is a follow-up message in
#  the same conversation, not a cold start.
#
#  For other tiers (Gemini, Ollama): cold-start per step with state
#  re-injection (no session support).
#
#  Depends on: models/step_tree.py, services/cli_common.py,
#              services/claude_code_executor.py, services/progress.py,
#              services/budget.py
#  Used by:    services/task_lifecycle.py

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from backend.config import cfg
from backend.models.enums import ModelTier
from backend.models.step_tree import StepNode, StepOutput, StepTree

logger = logging.getLogger("orchestration.tree_runner")


# ---------------------------------------------------------------------------
# Output parsing — extract named variables from model output
# ---------------------------------------------------------------------------

_OUTPUT_TAG_RE = re.compile(
    r"<output\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</output>",
    re.DOTALL,
)

_BRANCH_TAG_RE = re.compile(
    r"<branch_decision>\s*(.*?)\s*</branch_decision>",
    re.DOTALL,
)


def _parse_step_outputs(
    raw_output: str, expected: list[StepOutput],
) -> dict[str, Any]:
    """Extract named output variables from model response.

    The model is instructed to wrap each output in:
        <output name="var_name">value</output>

    Returns a dict mapping variable names to their values.
    Missing required outputs raise ValueError.
    """
    found: dict[str, str] = {}
    for match in _OUTPUT_TAG_RE.finditer(raw_output):
        name = match.group(1).strip()
        value = match.group(2).strip()
        found[name] = value

    result: dict[str, Any] = {}
    for out in expected:
        if out.name in found:
            val = found[out.name]
            # Try JSON parse for structured outputs
            if out.schema:
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    pass  # Keep as string — validation happens downstream
            result[out.name] = val
        elif out.required:
            raise ValueError(
                f"Step output '{out.name}' is required but was not produced. "
                f"Model must wrap output in <output name=\"{out.name}\">...</output>"
            )
    return result


def _parse_branch_decision(raw_output: str) -> str | None:
    """Extract the branch decision from model output."""
    match = _BRANCH_TAG_RE.search(raw_output)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Step prompt builder
# ---------------------------------------------------------------------------

def _build_step_prompt(
    step: StepNode,
    state: dict[str, Any],
    step_number: int,
    total_steps: int,
    task_description: str,
    *,
    is_first_step: bool = False,
    is_conversational: bool = False,
) -> str:
    """Build the prompt for a single step execution.

    When conversational (Claude Code session), the model already has
    context from prior steps — so we skip re-injecting state variables
    and the task description. Just give the next instruction.

    When cold-start (Gemini, Ollama), we inject everything.
    """
    parts: list[str] = []

    if is_first_step:
        # First step always gets full context
        parts.append(
            f"You are executing a structured task with {total_steps} steps. "
            f"I will give you one step at a time. Complete each step fully before I give you the next.\n\n"
            f"Overall task: {task_description}"
        )
    elif not is_conversational:
        # Cold-start tiers need context re-injection
        parts.append(
            f"You are executing step {step_number}/{total_steps} of a structured task.\n"
            f"Task context: {task_description[:500]}"
        )
        # Inject state variables this step needs
        if step.inputs:
            input_block: list[str] = []
            for var_name in step.inputs:
                if var_name in state:
                    val = state[var_name]
                    if isinstance(val, (dict, list)):
                        val = json.dumps(val, indent=2)
                    input_block.append(f"  {var_name}: {val}")
            if input_block:
                parts.append(
                    "Results from prior steps:\n" + "\n".join(input_block)
                )
    else:
        # Conversational follow-up — model remembers everything
        parts.append(f"Step {step_number}/{total_steps}:")

    # The step instruction
    parts.append(f"YOUR TASK:\n{step.instruction}")

    # Output requirements
    if step.outputs:
        output_lines: list[str] = []
        for out in step.outputs:
            req = "REQUIRED" if out.required else "OPTIONAL"
            output_lines.append(
                f'  <output name="{out.name}">{out.description}</output>  [{req}]'
            )
        parts.append(
            "When done, produce each output wrapped in XML tags exactly as shown:\n"
            + "\n".join(output_lines)
        )

    # Branch evaluation
    if step.branch_conditions:
        condition_lines: list[str] = []
        for bc in step.branch_conditions:
            condition_lines.append(f'  - If {bc.condition} → "{bc.target_step_id}"')
        if step.fallback_step_id:
            condition_lines.append(f'  - Otherwise → "{step.fallback_step_id}"')
        parts.append(
            "After producing your outputs, evaluate which condition applies:\n"
            + "\n".join(condition_lines)
            + "\n\nWrap your choice in: <branch_decision>step_id</branch_decision>"
        )

    # Sub-step generation (bounded dynamic)
    if step.allow_substeps and step.substep_template:
        parts.append(
            f"You may decompose this into sub-steps (max {step.max_substeps}).\n"
            f"Format:\n{step.substep_template}\n"
            "Sub-steps may not recurse or spawn further sub-steps."
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude Code conversational session (via CLIProvider)
# ---------------------------------------------------------------------------


async def _run_claude_session_step(
    prompt: str,
    session_id: str,
    *,
    is_first: bool,
    cwd: str | None,
    model: str | None = None,
    task_id: str = "",
    project_id: str = "",
    progress=None,
) -> dict:
    """Execute one step in a Claude Code conversation session.

    First step: starts the session with --session-id.
    Subsequent steps: resumes with --resume <session-id>.

    Delegates to CLIProvider for command construction, environment setup,
    subprocess management, and stream parsing.

    Returns: {output, prompt_tokens, completion_tokens, cost_usd, model_used}
    """
    from backend.services.cli_provider import CLIProvider, StreamEvent, StreamEventType

    provider = CLIProvider("claude_code", model=model)

    # Build a progress-forwarding callback
    async def _on_event(event: StreamEvent) -> None:
        if not progress:
            return
        if event.type == StreamEventType.ASSISTANT and event.content:
            preview = event.content[:200] + "..." if len(event.content) > 200 else event.content
            await progress.push_event(
                project_id, "task_output", preview, task_id=task_id,
            )
        elif event.type == StreamEventType.TOOL_USE:
            await progress.push_event(
                project_id, "tool_call", f"Using {event.tool_name}",
                task_id=task_id, tool=event.tool_name,
            )

    if is_first:
        result = await provider.start_session(
            prompt,
            session_id=session_id,
            cwd=cwd,
            on_event=_on_event,
        )
    else:
        result = await provider.resume_session(
            prompt,
            session_id=session_id,
            cwd=cwd,
            on_event=_on_event,
        )

    if not result.success and not result.output:
        raise RuntimeError(
            f"Claude Code session step failed (exit {result.exit_code}): {result.stderr[:500]}"
        )

    if result.timed_out:
        raise asyncio.TimeoutError(
            f"Claude Code session step timed out for task {task_id}"
        )

    return {
        "output": result.output,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost_usd": result.cost_usd,
        "model_used": result.model_used,
    }


# ---------------------------------------------------------------------------
# Cold-start step execution (Gemini, Ollama, etc.)
# ---------------------------------------------------------------------------

async def _execute_cold_start_step(
    step_prompt: str,
    task_row: dict,
    tier: ModelTier,
    *,
    db,
    budget,
    progress,
    http_client=None,
    tool_registry=None,
) -> dict:
    """Execute a single step as a cold start (no conversation memory)."""
    step_row = dict(task_row)
    step_row["description"] = step_prompt
    step_row["context_json"] = "[]"

    if tier == ModelTier.OLLAMA:
        from backend.services.ollama_agent import run_ollama_task
        return await run_ollama_task(
            task_row=step_row, http_client=http_client, budget=budget,
            tool_registry=tool_registry,
        )
    elif tier == ModelTier.GEMINI_CLI:
        from backend.services.generic_cli_executor import run_gemini_cli_task
        return await run_gemini_cli_task(
            task_row=step_row, db=db, budget=budget, progress=progress,
        )
    elif tier == ModelTier.CODEX_CLI:
        from backend.services.generic_cli_executor import run_codex_cli_task
        return await run_codex_cli_task(
            task_row=step_row, db=db, budget=budget, progress=progress,
        )
    else:
        # Fallback: Claude Code without session (shouldn't normally reach here)
        from backend.services.claude_code_executor import run_claude_code_task
        return await run_claude_code_task(
            task_row=step_row, db=db, budget=budget, progress=progress,
            model=tier.value if tier not in (ModelTier.CLAUDE_CODE,) else None,
        )


# ---------------------------------------------------------------------------
# Tier capability detection
# ---------------------------------------------------------------------------

_CONVERSATIONAL_TIERS = {
    ModelTier.CLAUDE_CODE,
    # API tiers route through Claude Code CLI, so they get sessions too
    ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS,
}


def _is_conversational(tier: ModelTier) -> bool:
    """Does this tier support conversation sessions (resume)?"""
    return tier in _CONVERSATIONAL_TIERS


# ---------------------------------------------------------------------------
# Tree walker — the main loop
# ---------------------------------------------------------------------------

async def run_step_tree(
    *,
    step_tree: dict | StepTree,
    task_row: dict,
    tier: ModelTier,
    db,
    budget,
    progress,
    http_client=None,
    tool_registry=None,
) -> dict:
    """Walk a step tree, executing each node through the model.

    For conversational tiers (Claude Code): maintains a single session
    across all steps. The model keeps its full conversation context —
    files read, code written, prior reasoning. Each step is a follow-up
    message, not a cold start.

    For cold-start tiers (Gemini, Ollama): each step is independent,
    with state variables re-injected into the prompt.

    Returns the same result dict format as single-shot executors.
    """
    # sqlite3.Row doesn't support .get() — normalize to dict
    if not isinstance(task_row, dict):
        task_row = dict(task_row)

    if isinstance(step_tree, dict):
        tree = StepTree.from_dict(step_tree)
    else:
        tree = step_tree

    task_id = task_row.get("id", "unknown")
    project_id = task_row.get("project_id", "unknown")
    task_description = task_row.get("description", "")
    conversational = _is_conversational(tier)

    # Session ID for conversational tiers
    session_id = str(uuid.uuid4()) if conversational else ""

    # Resolve cwd once for the whole tree walk
    cwd: str | None = None
    if conversational:
        from backend.services.cli_common import resolve_cwd
        cwd = await resolve_cwd(db, project_id)

    # Determine model override for API tiers routed through Claude Code
    model_override: str | None = None
    if tier not in (ModelTier.CLAUDE_CODE,) and conversational:
        model_override = tier.value

    # Accumulated state across steps
    state: dict[str, Any] = dict(tree.variables)

    # Accumulated costs
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost_usd = 0.0
    model_used = ""

    # Step execution tracking
    steps_executed = 0
    step_outputs_log: list[dict] = []
    iteration_counts: dict[str, int] = {}

    current_step = tree.get_step(tree.entry_step_id)
    if not current_step:
        return {
            "output": f"ERROR: Entry step '{tree.entry_step_id}' not found in tree.",
            "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0, "model_used": "", "budget_exhausted": False,
        }

    logger.info(
        "tree_runner: starting tree walk for task %s (%d steps, entry=%s, conversational=%s)",
        task_id, len(tree.steps), tree.entry_step_id, conversational,
    )

    while current_step and steps_executed < tree.max_total_steps:
        step_id = current_step.id
        steps_executed += 1
        is_first = steps_executed == 1

        # Track iterations for loop nodes
        iteration_counts[step_id] = iteration_counts.get(step_id, 0) + 1
        if iteration_counts[step_id] > current_step.max_iterations:
            logger.warning(
                "tree_runner: step %s exceeded max_iterations (%d), advancing",
                step_id, current_step.max_iterations,
            )
            current_step = tree.next_linear_step(step_id)
            continue

        # Emit step-level progress event
        await progress.push_event(
            project_id, "tree_step",
            f"Step {steps_executed}/{len(tree.steps)}: {step_id}",
            task_id=task_id,
            step_id=step_id,
            step_number=steps_executed,
            total_steps=len(tree.steps),
        )

        # Build step prompt
        step_prompt = _build_step_prompt(
            step=current_step,
            state=state,
            step_number=steps_executed,
            total_steps=len(tree.steps),
            task_description=task_description,
            is_first_step=is_first,
            is_conversational=conversational,
        )

        logger.info(
            "tree_runner: executing step %s (%d/%d) for task %s",
            step_id, steps_executed, len(tree.steps), task_id,
        )

        # Execute through the appropriate path
        t0 = time.time()
        try:
            if conversational:
                result = await _run_claude_session_step(
                    prompt=step_prompt,
                    session_id=session_id,
                    is_first=is_first,
                    cwd=cwd,
                    model=model_override,
                    task_id=task_id,
                    project_id=project_id,
                    progress=progress,
                )
            else:
                result = await _execute_cold_start_step(
                    step_prompt=step_prompt,
                    task_row=task_row,
                    tier=tier,
                    db=db, budget=budget, progress=progress,
                    http_client=http_client, tool_registry=tool_registry,
                )
        except Exception as e:
            # Step-level error — log it and let the caller handle retry
            logger.error(
                "tree_runner: step %s failed for task %s: %s",
                step_id, task_id, e,
            )
            step_outputs_log.append({
                "step_id": step_id,
                "status": "error",
                "elapsed_s": round(time.time() - t0, 2),
                "error": str(e)[:500],
            })
            # Bubble up — task_lifecycle's transient error handler will
            # retry the entire task. Future: checkpoint mid-tree.
            raise

        elapsed = time.time() - t0

        # Accumulate costs
        total_prompt_tokens += result.get("prompt_tokens", 0)
        total_completion_tokens += result.get("completion_tokens", 0)
        total_cost_usd += result.get("cost_usd", 0.0)
        model_used = result.get("model_used", model_used)

        raw_output = result.get("output", "")

        # Budget exhaustion mid-tree
        if result.get("budget_exhausted"):
            logger.warning(
                "tree_runner: budget exhausted at step %s for task %s",
                step_id, task_id,
            )
            step_outputs_log.append({
                "step_id": step_id,
                "status": "budget_exhausted",
                "elapsed_s": round(elapsed, 2),
                "raw_output_preview": raw_output[:500],
            })
            return {
                "output": _format_final_output(state, step_outputs_log, completed=False),
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "cost_usd": total_cost_usd,
                "model_used": model_used,
                "budget_exhausted": True,
            }

        # Record spend per step for audit
        await budget.record_spend(
            cost_usd=result.get("cost_usd", 0.0),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            provider="claude_code_cli" if conversational else tier.value,
            model=model_used,
            purpose=f"tree_step:{step_id}",
            project_id=project_id,
            task_id=task_id,
        )

        # Parse outputs from model response
        step_vars: dict[str, Any] = {}
        try:
            step_vars = _parse_step_outputs(raw_output, current_step.outputs)
            state.update(step_vars)
            step_status = "completed"
        except ValueError as e:
            logger.warning(
                "tree_runner: step %s output parse failed: %s", step_id, e,
            )
            # Store raw output — the model did the work, it just didn't
            # wrap it in tags. For conversational sessions this is fine
            # because the model remembers what it did regardless.
            state[f"_raw_{step_id}"] = raw_output
            step_status = "output_parse_failed"

        step_outputs_log.append({
            "step_id": step_id,
            "status": step_status,
            "elapsed_s": round(elapsed, 2),
            "variables_produced": list(step_vars.keys()) if step_status == "completed" else [],
            "raw_output_preview": raw_output[:300],
        })

        await progress.push_event(
            project_id, "tree_step_complete",
            f"Step {step_id}: {step_status} ({elapsed:.1f}s)",
            task_id=task_id,
            step_id=step_id,
            step_status=step_status,
            elapsed_s=round(elapsed, 2),
        )

        # Determine next step
        current_step = _resolve_next_step(current_step, raw_output, tree)

    if steps_executed >= tree.max_total_steps:
        logger.warning(
            "tree_runner: hit max_total_steps (%d) for task %s",
            tree.max_total_steps, task_id,
        )

    logger.info(
        "tree_runner: completed tree walk for task %s — %d steps, $%.4f, session=%s",
        task_id, steps_executed, total_cost_usd,
        session_id[:8] if session_id else "none",
    )

    return {
        "output": _format_final_output(state, step_outputs_log, completed=True),
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "cost_usd": total_cost_usd,
        "model_used": model_used,
        "budget_exhausted": False,
    }


# ---------------------------------------------------------------------------
# Next-step resolution
# ---------------------------------------------------------------------------

def _resolve_next_step(
    current: StepNode, raw_output: str, tree: StepTree,
) -> StepNode | None:
    """Determine the next step after current completes.

    Priority:
    1. Branch conditions (model-evaluated)
    2. Explicit next_step_id
    3. Linear order
    """
    if current.branch_conditions:
        decision = _parse_branch_decision(raw_output)
        if decision:
            legal_targets = {bc.target_step_id for bc in current.branch_conditions}
            if current.fallback_step_id:
                legal_targets.add(current.fallback_step_id)
            if decision in legal_targets:
                step = tree.get_step(decision)
                if step:
                    return step
                logger.warning(
                    "tree_runner: branch decision '%s' references unknown step", decision,
                )
            else:
                logger.warning(
                    "tree_runner: branch decision '%s' not in legal targets %s",
                    decision, legal_targets,
                )
        if current.fallback_step_id:
            return tree.get_step(current.fallback_step_id)

    if current.next_step_id:
        return tree.get_step(current.next_step_id)

    return tree.next_linear_step(current.id)


# ---------------------------------------------------------------------------
# Final output formatting
# ---------------------------------------------------------------------------

def _format_final_output(
    state: dict[str, Any],
    step_log: list[dict],
    *,
    completed: bool,
) -> str:
    """Format the accumulated state and step log into the task output."""
    parts: list[str] = []

    status = "COMPLETED" if completed else "PARTIAL (budget exhausted)"
    parts.append(f"## Tree Execution: {status}")
    parts.append(f"Steps executed: {len(step_log)}")

    parts.append("\n### Step Log")
    for entry in step_log:
        status_str = entry["status"]
        elapsed = entry.get("elapsed_s", "?")
        parts.append(f"- **{entry['step_id']}**: {status_str} ({elapsed}s)")
        if entry.get("variables_produced"):
            parts.append(f"  Produced: {', '.join(entry['variables_produced'])}")
        if entry.get("error"):
            parts.append(f"  Error: {entry['error']}")

    parts.append("\n### Accumulated State")
    for key, value in state.items():
        if key.startswith("_raw_"):
            continue
        if isinstance(value, (dict, list)):
            parts.append(f"**{key}:**\n```json\n{json.dumps(value, indent=2)}\n```")
        else:
            val_str = str(value)
            if len(val_str) > 2000:
                val_str = val_str[:2000] + "\n[...truncated...]"
            parts.append(f"**{key}:** {val_str}")

    return "\n".join(parts)
