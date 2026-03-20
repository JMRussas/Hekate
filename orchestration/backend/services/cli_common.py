#  Orchestration Engine - CLI Common Utilities
#
#  Shared functions used by all CLI executors (Claude Code, Gemini, Codex).
#  Single source of truth for prompt building, cwd resolution, and process
#  crash detection.
#
#  Depends on: services/prompt_renderer.py
#  Used by:    services/claude_code_executor.py, services/generic_cli_executor.py,
#              services/claude_agent.py, services/ollama_agent.py

import json
import logging
import re

from backend.services.prompt_renderer import (
    ContextEntry,
    ContextType,
    FewShotExample,
    PromptSpec,
    render_prompt,
)

logger = logging.getLogger("orchestration.executor")

# Windows crash exit codes that indicate resource exhaustion or process
# corruption — retryable because a fresh process may succeed.
_WINDOWS_CRASH_CODES = {
    0xC0000005,  # 3221225477 — access violation
    0xC0000409,  # 3221226505 — stack buffer overrun
    0xC00000FD,  # 3221225725 — stack overflow
    0xC0000142,  # DLL init failure (handle exhaustion)
}


def is_process_crash(returncode: int) -> bool:
    """True if the exit code indicates a process crash (not a logical error)."""
    if returncode is None:
        return False
    # Unsigned comparison for Windows negative codes
    unsigned = returncode & 0xFFFFFFFF if returncode < 0 else returncode
    return unsigned in _WINDOWS_CRASH_CODES


_CODE_TASK_TYPES = ("code", "integration", "game_code", "game_ui")

_EXECUTION_RULES = (
    "CRITICAL REQUIREMENT — READ THIS BEFORE DOING ANYTHING ELSE:\n\n"
    "This is a CODE task. You MUST use the Write tool or Edit tool to create or "
    "modify files. Your job is to produce working code ON DISK, not to describe "
    "what code should look like.\n\n"
    "FORBIDDEN: Writing a text description, plan, report, or summary of what "
    "the code should do. This will be rejected.\n\n"
    "REQUIRED: Call the Write or Edit tool at least once to create or modify "
    "a source file. If you finish without having written or edited any file, "
    "you have failed the task.\n\n"
    "Example of WRONG output: 'Here is the migration that renames the table...'\n"
    "Example of RIGHT output: Use the Write tool to create the .py file with "
    "the actual code."
)

_CODE_TASK_FEW_SHOT = FewShotExample(
    user_input="Create an Alembic migration that adds a status column to the tasks table",
    expected_output=(
        "I'll create the migration file using the Write tool.\n"
        "[Calls Write tool to create orchestration/backend/migrations/versions/025_add_status_column.py "
        "with proper revision chain, upgrade(), and downgrade()]"
    ),
    label="code task",
)


def _format_knowledge_block(content: list) -> str:
    """Format a project_knowledge content list into structured text.

    Produces a structured block that foregrounds *why* decisions were made,
    what alternatives were rejected, and how confident the finding is.
    This lets downstream tasks avoid repeating failed approaches and
    build on proven strategies.
    """
    lines: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        finding = item.get("finding", "")
        rationale = item.get("rationale")
        alternatives = item.get("alternatives_considered")
        confidence = item.get("confidence")
        category = item.get("category", "unknown")
        source = item.get("source_task_title")

        confidence_attr = f' confidence="{confidence}"' if confidence else ""
        source_attr = f' source="{source}"' if source else ""
        lines.append(f'  <finding category="{category}"{confidence_attr}{source_attr}>')
        lines.append(f"    <statement>{finding}</statement>")
        if rationale:
            lines.append(f"    <rationale>{rationale}</rationale>")
        if alternatives:
            lines.append(f"    <rejected_alternatives>{alternatives}</rejected_alternatives>")
        lines.append("  </finding>")
    return "\n".join(lines)


def _map_context_type(ctx_type: str) -> ContextType:
    """Map a raw context type string to a ContextType enum."""
    _TYPE_MAP = {
        "project_knowledge": ContextType.PROJECT_KNOWLEDGE,
        "historical_rationale": ContextType.HISTORICAL_RATIONALE,
        "execution_rules": ContextType.EXECUTION_RULES,
        "platform_context": ContextType.PLATFORM_CONTEXT,
        "verification_criteria": ContextType.VERIFICATION_CRITERIA,
        "sibling_tasks": ContextType.SIBLING_TASKS,
        "task_description": ContextType.TASK_DESCRIPTION,
        "meta_instructions": ContextType.META_INSTRUCTIONS,
        "dependency_output": ContextType.DEPENDENCY_OUTPUT,
        "target_signature": ContextType.CSHARP_WORKER,
        "available_methods": ContextType.CSHARP_WORKER,
        "constructor_params": ContextType.CSHARP_WORKER,
    }
    return _TYPE_MAP.get(ctx_type, ContextType.GENERIC)


def build_prompt_spec(task_row) -> PromptSpec:
    """Build a model-agnostic PromptSpec from a task database row.

    Converts the task_row's system_prompt, context_json, description,
    task_type, and tools into typed ContextEntry objects with priority.

    This is the shared spec builder used by all executors. Individual
    executors may further enrich the spec (e.g., claude_agent adds
    project knowledge from DB).
    """
    # sqlite3.Row doesn't support .get() — normalize to dict
    if not isinstance(task_row, dict):
        task_row = dict(task_row)

    identity = task_row["system_prompt"] or "You are a focused task executor."

    # Parse context entries
    context_json = task_row["context_json"] or "[]"
    raw_context = json.loads(context_json) if isinstance(context_json, str) else context_json
    context_entries: list[ContextEntry] = []

    for ctx in raw_context:
        ctx_type = re.sub(r"[^a-zA-Z0-9_]", "_", ctx.get("type", "context"))

        if ctx_type == "project_knowledge":
            content = ctx.get("content")
            if isinstance(content, list):
                block = _format_knowledge_block(content)
                if block:
                    context_entries.append(ContextEntry(
                        type=ContextType.HISTORICAL_RATIONALE,
                        tag="historical_rationale",
                        content=(
                            "HISTORICAL RATIONALE — Lessons from prior tasks in this project.\n"
                            "Each finding includes WHY the decision was made, what alternatives\n"
                            "were considered and rejected, and a confidence level.\n\n"
                            "INSTRUCTIONS: Use this rationale to inform your approach.\n"
                            "- Do NOT repeat strategies marked as failed or low-confidence.\n"
                            "- Build on approaches marked as high-confidence.\n"
                            "- When a rejected alternative is listed, do not revisit it unless\n"
                            "  you have new information that invalidates the original reasoning.\n\n"
                            + block
                        ),
                    ))
            elif isinstance(content, str) and content:
                context_entries.append(ContextEntry(
                    type=ContextType.PROJECT_KNOWLEDGE,
                    tag="project_knowledge",
                    content=content,
                ))
        else:
            content = ctx.get("content", "")
            if content:
                context_entries.append(ContextEntry(
                    type=_map_context_type(ctx_type),
                    tag=ctx_type,
                    content=content,
                ))

    # Detect code task and add execution rules + few-shot
    task_type = task_row.get("task_type", "") or ""
    tools_raw = task_row.get("tools_json") or task_row.get("tools", "[]") or "[]"
    tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw

    constraints: list[str] = []
    few_shot: list[FewShotExample] = []

    is_code_task = task_type in _CODE_TASK_TYPES and "write_file" in tools
    if is_code_task:
        context_entries.append(ContextEntry(
            type=ContextType.EXECUTION_RULES,
            tag="execution_rules",
            content=_EXECUTION_RULES,
        ))
        few_shot.append(_CODE_TASK_FEW_SHOT)
        constraints.append("You MUST write files using Write/Edit tools — text descriptions will be rejected.")

    # Inject execution intelligence from historical learnings
    try:
        from backend.services.learning.execution_learner import get_learner
        learner = get_learner()
        model_used = task_row.get("model_used") or ""
        historical_context = learner.format_historical_context(task_type, model_used)
        if historical_context:
            context_entries.append(ContextEntry(
                type=ContextType.EXECUTION_INTELLIGENCE,
                tag="execution_intelligence",
                content=historical_context,
            ))
    except Exception:
        pass  # Learning module unavailable — prompts work fine without it

    return PromptSpec(
        role="task_executor",
        identity=identity,
        task_description=task_row["description"] or "",
        context=context_entries,
        constraints=constraints,
        output_format="code" if is_code_task else "text",
        few_shot_examples=few_shot,
        assistant_prefill=(
            "I'll start by reading the relevant files, then write the implementation."
            if is_code_task else ""
        ),
        task_type=task_type,
        tools_available=tools,
    )


def build_prompt_for_provider(task_row, provider: str) -> str:
    """Build a prompt rendered for a specific provider. Returns flat_prompt string.

    Args:
        task_row: Task database row.
        provider: Provider name ("claude", "gemini", "ollama").
    """
    spec = build_prompt_spec(task_row)
    rendered = render_prompt(spec, provider)
    return rendered.flat_prompt


def build_prompt(task_row) -> str:
    """Build the full prompt from task description and context.

    Backward-compatible wrapper — renders with ClaudeRenderer (matching
    the XML-tag format that all CLI executors have been using).
    """
    return build_prompt_for_provider(task_row, "claude")


async def resolve_cwd(db, project_id: str) -> str | None:
    """Look up the project's working directory.

    Prefers the worktree path (isolated copy) if the executor created one.
    Falls back to repo_path (shared repo).
    """
    # Check if executor has a worktree for this project
    try:
        from backend.services.executor import Executor
        # Access the singleton executor's worktree map
        # This is set by _ensure_project_branch() before any tasks dispatch
        import backend.container as _container
        executor = _container.Container.executor()
        worktree = executor._worktrees.get(project_id)
        if worktree:
            return worktree
    except Exception:
        pass  # Container not wired or executor not available

    # Fallback to repo_path
    try:
        row = await db.fetchone(
            "SELECT repo_path FROM projects WHERE id = $1",
            (project_id,),
        )
        if row and row["repo_path"]:
            return row["repo_path"]
    except Exception as e:
        logger.debug("Failed to resolve repo_path for project %s: %s", project_id, e)
    return None
