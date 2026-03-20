"""Hephaestus handler — git operations (staging, syntax check, commit).

Receives task_verified events and:
  1. Syntax-checks Python files
  2. Stages affected files via git add
  3. Emits files_staged or stage_failed
"""

from __future__ import annotations

import logging
import time
from typing import Any

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.hephaestus")


# ---------------------------------------------------------------------------
# Git operations (mocked in tests)
# ---------------------------------------------------------------------------

async def _git_add(files: list[str], cwd: str) -> bool:
    """Stage files via git add. Returns True on success."""
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        "git", "add", "--", *files,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.error("Hephaestus: git add timed out after 30s for %d files in %s", len(files), cwd)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    if proc.returncode != 0:
        logger.error("Hephaestus: git add failed (rc=%d) stderr=%s", proc.returncode, (stderr or b"").decode(errors="replace")[:200])
        return False
    return True


async def _syntax_check(files: list[str], cwd: str = ".") -> dict:
    """Check Python file syntax. Returns {passed: bool, error: str?}."""
    import os
    for f in files:
        if not f.endswith(".py"):
            continue
        full_path = os.path.join(cwd, f) if not os.path.isabs(f) else f
        try:
            with open(full_path, "r") as fh:
                compile(fh.read(), full_path, "exec")
        except SyntaxError as e:
            return {"passed": False, "error": f"SyntaxError in {f}: {e}"}
        except FileNotFoundError:
            continue  # File might not exist locally (remote worktree)
    return {"passed": True}


# ---------------------------------------------------------------------------
# hephaestus_stage — task_verified → stage files
# ---------------------------------------------------------------------------

async def hephaestus_stage(event: Event, db) -> list[Emit] | None:
    """Stage affected files after task verification.

    Receives task_verified with optional affected_files list.
    Runs syntax check on Python files, then git add.
    """
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    affected_files = event.payload.get("affected_files", [])

    if not affected_files:
        return None  # Nothing to stage

    # Resolve cwd
    proj_row = await db.fetchone(
        "SELECT repo_path FROM projects WHERE id = $1", (project_id,))
    cwd = proj_row.get("repo_path", ".") if proj_row else "."

    # Syntax check Python files
    py_files = [f for f in affected_files if f.endswith(".py")]
    if py_files:
        check = await _syntax_check(py_files, cwd=cwd)
        if not check["passed"]:
            return [Emit("stage_failed", {
                "task_id": task_id,
                "project_id": project_id,
                "error": check["error"],
            }, source="hephaestus")]

    # Stage files
    success = await _git_add(affected_files, cwd=cwd)
    if not success:
        return [Emit("stage_failed", {
            "task_id": task_id,
            "project_id": project_id,
            "error": "git add failed",
        }, source="hephaestus")]

    return [Emit("files_staged", {
        "task_id": task_id,
        "project_id": project_id,
        "files": affected_files,
    }, source="hephaestus")]
