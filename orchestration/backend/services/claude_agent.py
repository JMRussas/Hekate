#  Orchestration Engine - Claude Agent
#
#  Runs a single task via the Claude API with multi-turn tool support.
#  Extracted from executor.py for modularity.
#
#  Depends on: config.py, db/connection.py, services/budget.py,
#              services/model_router.py, tools/registry.py
#  Used by:    services/task_lifecycle.py

import json
import logging

from backend.config import API_TIMEOUT, KNOWLEDGE_INJECTION_MAX_CHARS, MAX_HISTORY_ROUNDS, MAX_TOOL_ROUNDS
from backend.models.enums import ModelTier
from backend.services.model_router import calculate_cost, get_model_id

logger = logging.getLogger("orchestration.executor")


def _extract_csharp_context(context: list[dict]) -> dict | None:
    """Extract C# method task context entries if present.

    Returns a dict with target_signature, available_methods, constructor_params,
    or None if this is not a C# method task.
    """
    result = {}
    for ctx in context:
        ctx_type = ctx.get("type", "")
        if ctx_type == "target_signature":
            result["target_signature"] = ctx.get("content", "")
        elif ctx_type == "available_methods":
            result["available_methods"] = ctx.get("content", "")
        elif ctx_type == "constructor_params":
            result["constructor_params"] = ctx.get("content", "")
    return result if "target_signature" in result else None


def _build_csharp_worker_prompt(csharp_ctx: dict, task_row: dict) -> str:
    """Build a structured WorkerInstruction prompt for C# method tasks."""
    signature = csharp_ctx.get("target_signature", "")
    available = csharp_ctx.get("available_methods", "None")
    ctor_params = csharp_ctx.get("constructor_params", "None")
    description = task_row.get("description", "")

    return (
        "<WorkerInstruction>\n"
        "  <Context>\n"
        f"    <TargetSignature>{signature}</TargetSignature>\n"
        f"    <InjectedDependencies>{ctor_params}</InjectedDependencies>\n"
        f"    <AvailableMethods>\n{available}\n    </AvailableMethods>\n"
        "  </Context>\n\n"
        f"  <LogicGoal>\n{description}\n  </LogicGoal>\n\n"
        "  <ExecutionRules>\n"
        "    <Rule>Output ONLY the method body. Do not wrap in a class or add using statements.</Rule>\n"
        "    <Rule>Use the AvailableMethods strictly. Do not hallucinate helper methods that don't exist.</Rule>\n"
        "    <Rule>If the LogicGoal requires a method that doesn't exist in AvailableMethods, "
        "note it clearly in your output so the orchestrator can create it.</Rule>\n"
        "    <Rule>Keep the implementation under 50 lines. If more is needed, "
        "split logic into private helpers and note them.</Rule>\n"
        "  </ExecutionRules>\n\n"
        "  <OutputConstraint>Return raw C# code only. No markdown formatting blocks.</OutputConstraint>\n"
        "</WorkerInstruction>"
    )


async def run_claude_task(
    *,
    task_row,
    est_cost: float = 0.0,
    client,
    tool_registry,
    budget,
    progress,
    db=None,
) -> dict:
    """Execute a task via the Claude API with tool support.

    Args:
        task_row: Task database row.
        est_cost: The original reserved cost estimate. Used for mid-loop
            budget checks — if actual spend exceeds the estimate, we verify
            the global budget hasn't been exhausted before continuing.
        client: anthropic.AsyncAnthropic instance.
        tool_registry: ToolRegistry for tool definitions and execution.
        budget: BudgetManager instance.
        progress: ProgressManager instance.
        db: Database instance (optional). Used to inject project knowledge
            into the system prompt when available.
    """
    tier = ModelTier(task_row["model_tier"])
    model_id = get_model_id(tier)
    task_id = task_row["id"]
    project_id = task_row["project_id"]

    # Build context
    context = json.loads(task_row["context_json"]) if task_row["context_json"] else []

    # Check for C# method task — uses structured WorkerInstruction prompt
    csharp_context = _extract_csharp_context(context)
    if csharp_context:
        system_parts = [_build_csharp_worker_prompt(csharp_context, task_row)]
    else:
        system_parts = [task_row["system_prompt"] or "You are a focused task executor."]

    system_parts.append(
        "\n<meta_instructions>\n"
        "If you discover any constraints, gotchas, API quirks, or architectural "
        "decisions during this task, note them clearly in your output so they can "
        "be preserved for other tasks.\n"
        "</meta_instructions>"
    )
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        if ctx_type in ("target_signature", "available_methods", "constructor_params"):
            continue  # Already injected into WorkerInstruction
        system_parts.append(f"\n<{ctx_type}>\n{ctx.get('content', '')}\n</{ctx_type}>")

    # Inject project knowledge from earlier tasks
    if db is not None:
        try:
            knowledge_rows = await db.fetchall(
                "SELECT category, content, source_task_title FROM project_knowledge "
                "WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
            if knowledge_rows:
                knowledge_parts = []
                total_chars = 0
                for kr in knowledge_rows:
                    entry = f"[{kr['category']}] {kr['content']}"
                    if kr["source_task_title"]:
                        entry += f" (from: {kr['source_task_title']})"
                    if total_chars + len(entry) > KNOWLEDGE_INJECTION_MAX_CHARS:
                        break
                    knowledge_parts.append(entry)
                    total_chars += len(entry)
                if knowledge_parts:
                    system_parts.append(
                        "\n<project_knowledge>\n"
                        "The following findings were discovered by earlier tasks "
                        "in this project. Use them to avoid repeating mistakes "
                        "and to maintain consistency:\n"
                        + "\n".join(f"- {p}" for p in knowledge_parts)
                        + "\n</project_knowledge>"
                    )
        except Exception as e:
            logger.debug("Failed to inject project knowledge: %s", e)

    system_prompt = "\n".join(system_parts)

    # Build tool definitions
    tool_names = json.loads(task_row["tools_json"]) if task_row["tools_json"] else []
    tools = tool_registry.get_many(tool_names)
    tool_defs = [t.to_claude_tool() for t in tools]
    tool_map = {t.name: t for t in tools}

    # Initial message
    messages = [{"role": "user", "content": task_row["description"]}]

    if client is None:
        raise RuntimeError("Executor not started — call start() before dispatching tasks")

    total_prompt = 0
    total_completion = 0
    total_cost = 0.0
    text_parts: list[str] = []
    budget_exhausted = False

    for round_num in range(MAX_TOOL_ROUNDS):
        # Make API call
        kwargs = {
            "model": model_id,
            "max_tokens": task_row["max_tokens"],
            "system": system_prompt,
            "messages": messages,
            "timeout": API_TIMEOUT,
        }
        if tool_defs:
            kwargs["tools"] = tool_defs

        response = await client.messages.create(**kwargs)

        # Record usage
        pt = response.usage.input_tokens
        ct = response.usage.output_tokens
        cost = calculate_cost(model_id, pt, ct)
        total_prompt += pt
        total_completion += ct
        total_cost += cost

        await budget.record_spend(
            cost_usd=cost,
            prompt_tokens=pt,
            completion_tokens=ct,
            provider="anthropic",
            model=model_id,
            purpose="execution",
            project_id=project_id,
            task_id=task_id,
        )

        # Per-round budget check: if actual cost exceeded the original estimate,
        # verify that global budget hasn't been exhausted before continuing.
        if total_cost > est_cost and not await budget.can_spend(0.001):
            logger.warning(
                "Budget exhausted mid-tool-loop for task %s after %d rounds, "
                "returning partial result",
                task_id, round_num + 1,
            )
            budget_exhausted = True

        # Process response
        has_tool_use = False
        tool_results = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                has_tool_use = True
                tool_name = block.name
                tool_input = block.input

                await progress.push_event(
                    project_id, "tool_call", f"Calling {tool_name}",
                    task_id=task_id, tool=tool_name,
                )

                # Auto-inject project_id for file tools
                if tool_name in ("read_file", "write_file"):
                    tool_input["project_id"] = project_id

                # Execute tool
                tool = tool_map.get(tool_name)
                if tool:
                    try:
                        result = await tool.execute(tool_input)
                    except Exception as e:
                        # Sanitize error — don't leak internal paths or stack traces
                        result = f"Tool error: {type(e).__name__}: operation failed"
                        logger.debug("Tool %s error detail: %s", tool_name, e)
                else:
                    result = f"Unknown tool: {tool_name}"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not has_tool_use or budget_exhausted:
            break

        # Feed tool results back
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        # Prune old rounds to prevent quadratic token growth.
        # Keep the first user message (task description) + last N rounds
        # (each round = assistant + user messages).
        max_msgs = 1 + (MAX_HISTORY_ROUNDS * 2)
        if len(messages) > max_msgs:
            messages = [messages[0]] + messages[-(MAX_HISTORY_ROUNDS * 2):]

    return {
        "output": "\n".join(text_parts),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "cost_usd": round(total_cost, 6),
        "model_used": model_id,
        "budget_exhausted": budget_exhausted,
    }
