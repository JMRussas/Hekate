#  Orchestration Engine - CLI Common Utilities
#
#  Shared functions used by all CLI executors (Claude Code, Gemini, Codex).
#  Single source of truth for prompt building, cwd resolution, and process
#  crash detection.
#
#  Depends on: (none — pure utilities, DB passed as arg)
#  Used by:    services/claude_code_executor.py, services/generic_cli_executor.py

import json
import logging
import re

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


def build_prompt(task_row) -> str:
    """Build the full prompt from task description and context.

    Used by all CLI executors. Context entries are wrapped in XML tags
    for structured prompt sections.
    """
    parts = []

    system_prompt = task_row["system_prompt"] or ""
    if system_prompt:
        parts.append(system_prompt)

    context_json = task_row["context_json"] or "[]"
    context = json.loads(context_json) if isinstance(context_json, str) else context_json
    for ctx in context:
        # Sanitize tag name to prevent prompt injection via crafted context types
        ctx_type = re.sub(r"[^a-zA-Z0-9_]", "_", ctx.get("type", "context"))
        content = ctx.get("content", "")
        if content:
            parts.append(f"<{ctx_type}>\n{content}\n</{ctx_type}>")

    parts.append(task_row["description"])
    return "\n\n".join(parts)


async def resolve_cwd(db, project_id: str) -> str | None:
    """Look up the project's repo_path for use as working directory."""
    try:
        row = await db.fetchone(
            "SELECT repo_path FROM projects WHERE id = ?",
            (project_id,),
        )
        if row and row["repo_path"]:
            return row["repo_path"]
    except Exception as e:
        logger.debug("Failed to resolve repo_path for project %s: %s", project_id, e)
    return None
