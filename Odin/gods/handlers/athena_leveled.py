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
from gods import safe_json
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
# Gateway call helper
# ---------------------------------------------------------------------------

async def _call_gateway(
    *,
    provider: str = "claude",
    model: str | None = None,
    system_prompt: str,
    user_message: str,
    gateway_url: str | None = None,
    timeout: float = 1800.0,  # 30 min — CLI sessions can run long, don't kill them
    history: list[dict] | None = None,
) -> str:
    """Call LLM Gateway, return text response.

    If history is provided, it is a list of prior turns:
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
    These are concatenated into the user_message so the model sees the full conversation.
    The gateway only accepts a single user_message string, so multi-turn is simulated
    by formatting the history inline.
    """
    import httpx
    from gods.config import GATEWAY_URL
    if not gateway_url:
        gateway_url = GATEWAY_URL

    if history:
        # Format prior turns inline so the model has full context
        prior = ""
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            label = "User" if role == "user" else "Assistant"
            prior += f"\n\n[{label}]: {content}"
        full_message = f"{prior}\n\n[User]: {user_message}"
    else:
        full_message = user_message

    try:
        body: dict = {
            "provider": provider,
            "system_prompt": system_prompt,
            "user_message": full_message,
        }
        if model:
            body["model"] = model
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{gateway_url}/v1/chat", json=body)
            resp.raise_for_status()
            return resp.json().get("text", "")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"LLM Gateway returned HTTP {e.response.status_code}: {e.response.text[:200]}"
        ) from e
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Cannot connect to LLM Gateway at {gateway_url}: {e}"
        ) from e
    except (httpx.ReadTimeout, httpx.TimeoutException, TimeoutError) as e:
        raise RuntimeError(
            f"LLM Gateway request timed out after {timeout}s"
        ) from e


# ---------------------------------------------------------------------------
# Internal functions — patched in tests, call gateway directly (no monolith)
# ---------------------------------------------------------------------------

async def _generate_l1(
    project_id: str,
    requirements: str,
    project_name: str,
    db,
    repo_path: str = ".",
    additional_repos: list[str] | None = None,
    **kwargs,
) -> dict:
    """Generate L1 plan via Claude CLI with hekate code analysis tools.

    The CLI session:
    1. Uses hekate-mcp tools to analyze the codebase
    2. Understands the existing architecture and patterns
    3. Produces a plan that fits within the codebase
    4. Returns structured JSON

    Returns: {"plan_id": str, "plan": dict}
    """
    import uuid
    from gods.providers.response_validator import extract_json
    from gods.providers.claude import ClaudeCodeProvider, ClaudeCodeConfig

    prompt = (
        f"# Planning Task: {project_name}\n\n"
        f"## Requirements\n{requirements}\n\n"
        f"## Instructions\n"
        f"1. Use the hekate code analysis tools (mcp__hekate__analyze_file, "
        f"mcp__hekate__find_usages, mcp__hekate__where, mcp__hekate__project_graph) "
        f"to understand the relevant parts of the codebase\n"
        f"2. Understand the existing architecture and patterns before planning\n"
        f"3. Create a plan that fits within the current codebase — don't invent new "
        f"patterns unless the task explicitly asks to change the architecture\n"
        f"4. Output your plan as a JSON block with this format:\n\n"
        f'```json\n'
        f'{{"summary": "...", "phases": [{{"name": "...", "tasks": ['
        f'{{"title": "...", "description": "...", "task_type": "code|research|test", '
        f'"depends_on": []}}]}}]}}\n'
        f'```\n\n'
        f"Rules:\n"
        f"- Break work into small, focused tasks\n"
        f"- Group related tasks into phases (phases execute as waves)\n"
        f"- depends_on is an array of task indices within the same phase\n"
        f"- task_type: 'code' for implementation, 'research' for analysis, 'test' for testing\n"
        f"- Reference actual files you found in the codebase, not guessed paths\n"
    )

    # Use CLI provider for planning — gives the model codebase access via hekate-mcp
    planner = ClaudeCodeProvider(ClaudeCodeConfig(
        allowed_tools=[
            "Read", "Glob", "Grep",
            "mcp__hekate__analyze_file",
            "mcp__hekate__find_usages",
            "mcp__hekate__find_implementations",
            "mcp__hekate__find_patterns",
            "mcp__hekate__where",
            "mcp__hekate__project_graph",
        ],
        mcp_config=ClaudeCodeProvider._build_mcp_config(),
        max_turns=20,
        dangerously_skip_permissions=True,
        add_dirs=additional_repos or None,
        append_system_prompt=(
            "You are a planner, not an executor. Do NOT modify any files. "
            "Read and analyze the codebase, then output a JSON plan. "
            "Your plan must reference real files and fit the existing architecture."
        ),
    ))

    try:
        result = await planner.execute(prompt=prompt, cwd=repo_path, timeout=1800)
        text = result.output
    except Exception as e:
        logger.warning("Athena: CLI planner failed (%s), falling back to gateway", e)
        text = await _call_gateway(
            system_prompt="You are a software project planner. Generate a structured plan as JSON.",
            user_message=f"Project: {project_name}\n\nRequirements:\n{requirements}",
        )

    plan = extract_json(text)
    if plan is None:
        plan = {"summary": "Could not parse plan", "phases": []}

    plan_id = uuid.uuid4().hex[:12]
    await db.execute_write(
        "INSERT OR REPLACE INTO plans (id, project_id, plan_json, level, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        (plan_id, project_id, json.dumps(plan), "L1", time.time()),
    )

    return {"plan_id": plan_id, "plan": plan}


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
    """Deepen plan to next level via LLM gateway. No monolith imports.

    Returns: {"plan_id": str, "plan": dict, "conversation_id": str}
    """
    import uuid
    from gods.providers.response_validator import extract_json

    level_descriptions = {
        PlanLevel.L2: "Add: detailed description, affected_files list, depends_on, complexity (simple/medium/complex) for each task.",
        PlanLevel.L3: "Add: implementation_notes, test_strategy, edge_cases list for each task.",
        PlanLevel.L4: "Add: exact changes list with {file, action, name, signature, returns} for each task.",
        PlanLevel.L5: "Add: full code body for each change.",
    }

    if not isinstance(current_plan, dict):
        current_plan = {}
    plan_data = current_plan.get("plan", current_plan)
    plan_json = json.dumps(plan_data, indent=2)

    system_prompt = (
        f"You are deepening a software plan to {target_level.name}.\n\n"
        f"Current plan:\n{plan_json[:3000]}\n\n"
        f"Deepen to {target_level.name}: {level_descriptions.get(target_level, '')}\n\n"
        "Return the COMPLETE updated plan as JSON in the same format.\n"
    )

    user_message = f"Requirements:\n{requirements}"
    if review_feedback:
        user_message += f"\n\nReview feedback to address:\n{review_feedback}"

    text = await _call_gateway(
        system_prompt=system_prompt,
        user_message=user_message,
    )

    plan = extract_json(text)
    if plan is None:
        plan = plan_data

    plan_id = current_plan.get("plan_id", uuid.uuid4().hex[:12])
    await db.execute_write(
        "INSERT OR REPLACE INTO plans (id, project_id, plan_json, level, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        (plan_id, project_id, json.dumps(plan), target_level.name, time.time()),
    )

    return {"plan_id": plan_id, "plan": plan, "conversation_id": conversation_id}


async def _thorough_review(
    plan: dict,
    requirements: str,
    project_name: str = "",
    level: PlanLevel = PlanLevel.L3,
    **kwargs,
) -> dict:
    """Thorough review by Model B via gateway. No monolith imports.

    Returns: {"approved": bool, "confidence": float, "feedback": str, "gaps": list}
    """
    from gods.providers.response_validator import validate_verdict

    plan_data = plan.get("plan", plan)
    plan_json = json.dumps(plan_data, indent=2)

    system_prompt = (
        "You are reviewing a software execution plan. Be critical.\n\n"
        "Questions to answer:\n"
        "1. Does every requirement have at least one task?\n"
        "2. Are there gaps — things implied but no task covers?\n"
        "3. Are dependencies correct?\n"
        "4. Are tasks too large or too small?\n"
        "5. Is this the right approach?\n\n"
        'Respond with JSON: {"verdict": "passed|gaps_found", "confidence": 0.0-1.0, '
        '"feedback": "specific feedback", "gaps": ["gap1", "gap2"]}\n'
    )

    text = await _call_gateway(
        system_prompt=system_prompt,
        user_message=f"Project: {project_name}\n\nRequirements:\n{requirements}\n\nPlan:\n{plan_json[:4000]}",
        provider="claude",
    )

    result = validate_verdict(text)

    return {
        "approved": result.get("verdict") == "passed",
        "confidence": result.get("confidence", 0.5),
        "feedback": result.get("feedback", ""),
        "gaps": result.get("gaps", []),
    }


async def _generate_tdd_tests(
    plan: dict,
    requirements: str,
    project_name: str = "",
    **kwargs,
) -> dict:
    """Generate TDD test specs. No monolith imports.

    Returns: {"test_specs": [{"task_id": str, "test_file": str, "test_cases": [...]}]}
    """
    tasks = []
    plan_data = plan.get("plan", plan)
    for phase in plan_data.get("phases", []):
        tasks.extend(phase.get("tasks", []))
    if not tasks:
        tasks = plan_data.get("tasks", [])

    code_tasks = [t for t in tasks if t.get("task_type") == "code"]

    test_specs = []
    for i, task in enumerate(code_tasks):
        title_slug = task.get("title", f"task_{i}").lower().replace(" ", "_")[:30]
        test_specs.append({
            "task_id": task.get("id", f"task-{i}"),
            "test_file": f"tests/test_{title_slug}.py",
            "test_cases": [
                f"test_{title_slug}_happy_path",
                f"test_{title_slug}_edge_cases",
            ],
        })

    return {"test_specs": test_specs}


async def _decompose_plan(
    project_id: str,
    plan: dict,
    plan_id: str,
    db,
    plan_config: dict | None = None,
) -> int:
    """Decompose plan into task rows. No monolith imports.

    Attaches Conductor-style TaskDefinition to each task's context_json
    based on task_type and complexity. If the task dict contains an explicit
    "task_definition" key (from L3+ planning), that overrides defaults.

    Returns the number of tasks created.
    """
    import uuid
    from gods.task_definition import (
        TaskDefinition, apply_defaults, merge_with_plan_config, get_registry,
    )

    registry = get_registry()
    now = time.time()
    all_tasks: list[dict] = []
    wave = 0

    for phase in plan.get("phases", []):
        phase_tasks = phase.get("tasks", [])
        offset = len(all_tasks)

        for i, task in enumerate(phase_tasks):
            task_id = uuid.uuid4().hex[:12]
            deps = []
            for dep_idx in task.get("depends_on", []):
                if isinstance(dep_idx, int) and 0 <= dep_idx < len(all_tasks):
                    deps.append(all_tasks[dep_idx]["id"])

            status = "pending" if wave == 0 and not deps else "blocked"

            # Validate against registry
            errors = registry.validate_task(task)
            if errors:
                logger.warning("Task '%s' validation: %s", task.get("title", "?"), "; ".join(errors))

            # Build TaskDefinition: registry → explicit override → type-based defaults
            task_type = task.get("task_type", "code")
            if "task_definition" in task:
                td = TaskDefinition.from_dict(task["task_definition"])
            elif task_type in registry:
                td = registry.get_definition(task_type, task.get("complexity", "medium"))
            else:
                td = apply_defaults(task_type, task.get("complexity", "medium"))

            # Merge with plan-level config if present
            td = merge_with_plan_config(
                td, plan_config,
                task_type=task_type,
                complexity=task.get("complexity", "medium"),
            )

            # Build context_json with task_definition embedded
            context = {"task_definition": td.to_dict()}

            all_tasks.append({
                "id": task_id,
                "title": task.get("title", f"Task {i}"),
                "description": task.get("description", ""),
                "task_type": task.get("task_type", "code"),
                "wave": wave,
                "deps": deps,
                "status": status,
                "context_json": json.dumps(context),
            })
        wave += 1

    for task in all_tasks:
        await db.execute_write(
            "INSERT OR IGNORE INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, wave, status, context_json, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
            (task["id"], project_id, plan_id, task["title"],
             task["description"], task["task_type"], task["wave"],
             task["status"], task["context_json"], now, now),
        )
        for dep_id in task["deps"]:
            await db.execute_write(
                "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
                (task["id"], dep_id),
            )

    return len(all_tasks)


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
                plan = safe_json.loads(plan, {})
            except (json.JSONDecodeError, TypeError):
                plan = {}
        for phase in plan.get("phases", []):
            raw_tasks.extend(phase.get("tasks", []))

    # First pass: assign IDs
    specs = []
    id_map: dict[int, str] = {}  # index → task ID
    for i, t in enumerate(raw_tasks):
        task_id = t.get("id", f"task-{i}")
        id_map[i] = task_id

    # Second pass: resolve depends_on integers to task IDs
    for i, t in enumerate(raw_tasks):
        raw_deps = t.get("depends_on") or []
        resolved_deps = []
        for dep in raw_deps:
            if isinstance(dep, int):
                # Integer index → resolve to task ID
                if dep in id_map:
                    resolved_deps.append(id_map[dep])
            elif isinstance(dep, str):
                resolved_deps.append(dep)

        specs.append(TaskSpec(
            id=id_map[i],
            title=t.get("title", ""),
            task_type=t.get("task_type", "code"),
            wave=t.get("wave", i // 3),
            description=t.get("description"),
            affected_files=t.get("affected_files"),
            depends_on=resolved_deps if resolved_deps else None,
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
        raw = safe_json.loads_dict(config_json)
        return PlanConfig(
            tdd=raw.get("tdd", True),
            narration=raw.get("narration", True),
            target_level=raw.get("target_level", "auto"),
            direct_write=raw.get("direct_write", True),
            max_concurrent=raw.get("max_concurrent", 2),
            use_node_tree_planner=raw.get("use_node_tree_planner", False),
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
        "SELECT name, requirements, status, config_json, repo_path, additional_repos "
        "FROM projects WHERE id = $1",
        (project_id,),
    )
    if not row:
        return [Emit("planning_failed", {
            "project_id": project_id, "error": "Project not found",
        }, source="athena")]

    project_name = row["name"]
    requirements = row.get("requirements", "") or ""
    status = row.get("status", "draft")
    repo_path = row.get("repo_path", ".") or "."
    config = _load_config(row.get("config_json"))

    # Parse additional repos (JSON array stored as TEXT)
    additional_repos = None
    raw_additional = row.get("additional_repos")
    if raw_additional:
        try:
            additional_repos = json.loads(raw_additional)
        except (json.JSONDecodeError, TypeError):
            pass

    # Guard: skip if already beyond draft
    if status not in ("draft",):
        return []

    # Guard: skip if project uses the new parallel node-tree planner
    if config.use_node_tree_planner:
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
                repo_path=repo_path,
                additional_repos=additional_repos,
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

            # Preserve the last good plan before attempting to deepen.
            # If deepening fails, we revert to this instead of using the
            # corrupted deeper plan (which may have 0 tasks).
            last_good_plan = current_plan

            level_reached = False
            for retry in range(MAX_RULE_RETRIES + 1):
                try:
                    deepened = await _deepen_plan(
                        project_id, current_plan, next_level,
                        conversation_id, requirements, db,
                    )
                except Exception as e:
                    narrate(f"{next_level.name} deepening failed: {e}")
                    if retry >= MAX_RULE_RETRIES:
                        break  # Exhausted retries — fall back to last good plan
                    continue  # Retry on transient failures (gateway timeout, etc.)

                conversation_id = deepened.get("conversation_id", conversation_id)

                # Rule check at new level
                tasks = _parse_tasks(deepened)
                rule_result = validate_plan(tasks, next_level, requirements=requirements)

                if rule_result.passed:
                    narrate(f"{next_level.name} plan passed rule check")
                    current_plan = deepened
                    current_level = next_level
                    level_reached = True
                    break
                else:
                    narrate(f"{next_level.name} rule check failed: {rule_result.reason}")
                    if retry >= MAX_RULE_RETRIES:
                        narrate(f"Could not reach {next_level.name}, proceeding at {current_level.name}")
                        break

            if not level_reached:
                # Revert to last good plan — don't use the corrupted deeper plan
                current_plan = last_good_plan
                break

        # ---------------------------------------------------------------
        # Step 4: Thorough review (Model B, fresh prompt)
        # Skip review for L1 — it's a quick plan, review adds latency
        # and often rejects over minor dependency nits.
        # ---------------------------------------------------------------
        review = {"approved": True, "confidence": 1.0, "feedback": "", "gaps": []}
        if current_level.value <= PlanLevel.L1.value:
            narrate("Skipping review for L1 plan (quick mode)")
        else:
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
        task_count = 0
        if plan_id:
            narrate("Decomposing final plan into executable tasks...")
            try:
                task_count = await _decompose_plan(
                    project_id, current_plan.get("plan", current_plan), plan_id, db,
                )
                narrate(f"Created {task_count} tasks from plan")
            except Exception as e:
                logger.error("Decomposition failed: %s", e)
                narrate(f"Decomposition failed: {e}")

        # If decomposition produced 0 tasks, fail the project — don't continue
        # to executing with nothing to execute.
        if task_count == 0:
            error_msg = "Plan decomposition produced 0 tasks"
            logger.error("Athena: %s for project %s", error_msg, project_id[:8])
            narrate(error_msg)
            await db.execute_write(
                "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                ("failed", time.time(), project_id),
            )
            return emits + [Emit("planning_failed", {
                "project_id": project_id,
                "error": error_msg,
            }, source="athena")]

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

        # Gate checks for plan_generated to verify plan exists in DB
        emits.append(Emit("plan_generated", {
            "plan_id": current_plan.get("plan_id", ""),
            "project_id": project_id,
        }, source="athena"))
        emits.append(Emit("project_planned", planned_payload, source="athena"))
        return emits

    except Exception as e:
        logger.error("Athena leveled: planning failed for %s: %s", project_id, e, exc_info=True)
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("failed", time.time(), project_id),
        )
        return emits + [Emit("planning_failed", {
            "project_id": project_id,
            "error": str(e),
        }, source="athena", severity="error")]


# ---------------------------------------------------------------------------
# athena_reassess_standalone — wave reassessment (no monolith imports)
# ---------------------------------------------------------------------------

async def athena_reassess_standalone(event: Event, db) -> list[Emit] | None:
    """Handle wave_complete → reassess via gateway → emit wave_assessed.

    No monolith imports. Calls LLM gateway directly.
    """
    from gods.providers.response_validator import extract_json

    project_id = event.payload.get("project_id")
    wave = event.payload.get("wave", 0)

    try:
        # Get completed task summaries
        tasks = await db.fetchall(
            "SELECT title, status, substr(output_text, 1, 500) as output_summary "
            "FROM tasks WHERE project_id = $1 AND wave = $2",
            (project_id, wave),
        )

        task_outcomes = json.dumps([dict(t) for t in tasks], indent=2) if tasks else "[]"

        # Check for a decision edge on this wave (include context for odin_decide)
        has_decision = False
        try:
            edge = await db.fetchone(
                "SELECT id FROM workflow_edges "
                "WHERE project_id = $1 AND source_wave = $2 AND edge_type = $3 AND status = $4",
                (project_id, wave, "decision", "pending"),
            )
            has_decision = edge is not None
        except Exception:
            pass  # Table may not exist yet

        text = await _call_gateway(
            system_prompt=(
                "You are evaluating a completed wave of tasks. Decide next steps.\n\n"
                'Respond with JSON: {"outcome": "continue|replan|escalate_to_human", '
                '"rationale": "why"}\n\n'
                "- continue: remaining tasks look good, proceed\n"
                "- replan: results suggest the plan needs revision\n"
                "- escalate_to_human: something unexpected happened\n"
            ),
            user_message=f"Wave {wave} completed.\n\nTask outcomes:\n{task_outcomes}",
        )

        result = extract_json(text) or {"outcome": "continue", "rationale": "Default: continue"}

        return [Emit("wave_assessed", {
            "project_id": project_id,
            "wave": wave,
            "outcome": result.get("outcome", "continue"),
            "rationale": result.get("rationale", ""),
            "has_decision_edge": has_decision,
        }, source="athena")]

    except Exception as e:
        logger.error("Athena: reassessment failed for %s wave %d: %s", project_id, wave, e)
        return [Emit("wave_assessed", {
            "project_id": project_id,
            "wave": wave,
            "outcome": "continue",
            "rationale": f"Reassessment failed ({e}), continuing",
        }, source="athena")]
