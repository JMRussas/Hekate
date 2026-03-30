"""Hephaestus handler — git operations (staging, syntax check, commit).

Receives task_verified events and:
  1. Syntax-checks Python files
  2. Stages affected files via git add
  3. Emits files_staged or stage_failed
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Git helpers for project completion
# ---------------------------------------------------------------------------

async def _git_run(args: list[str], cwd: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, "", "timeout"
    return (
        proc.returncode or 0,
        (stdout or b"").decode(errors="replace").strip(),
        (stderr or b"").decode(errors="replace").strip(),
    )


async def _has_staged_changes(cwd: str) -> bool:
    """Check if there are staged changes to commit."""
    rc, out, _ = await _git_run(["diff", "--cached", "--quiet"], cwd)
    return rc != 0  # exit 1 = there are differences


async def _get_current_branch(cwd: str) -> str:
    """Get the current git branch name."""
    rc, out, _ = await _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out if rc == 0 else "main"


# ---------------------------------------------------------------------------
# hephaestus_complete — project_complete → commit + push + PR
# ---------------------------------------------------------------------------

async def hephaestus_complete(event: Event, db) -> list[Emit] | None:
    """On project completion: commit staged changes, push branch, create PR.

    Steps:
      1. Check for staged changes — skip if nothing to commit
      2. Create a project branch if on main
      3. Commit with descriptive message
      4. Push to origin
      5. Create PR via gh cli
      6. Emit pr_created or commit_complete
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        return None

    proj_row = await db.fetchone(
        "SELECT name, repo_path FROM projects WHERE id = $1", (project_id,),
    )
    if not proj_row:
        return None

    project_name = proj_row.get("name", "unknown")
    cwd = proj_row.get("repo_path", ".")
    if not cwd or cwd == ".":
        logger.warning("Hephaestus: no repo_path for project %s, skipping commit", project_id[:8])
        return None

    # Gather all affected files from completed tasks (not git add -u which
    # would stage unrelated changes from other projects or local edits)
    task_files = await db.fetchall(
        "SELECT output_text, context_json FROM tasks "
        "WHERE project_id = $1 AND status = $2",
        (project_id, "completed"),
    )
    all_affected: set[str] = set()
    for tf in task_files:
        # Extract affected_files from context_json if available
        ctx_str = tf.get("context_json") or "{}"
        try:
            ctx = json.loads(ctx_str) if isinstance(ctx_str, str) else (ctx_str or {})
            for f in ctx.get("affected_files", []):
                if f:
                    all_affected.add(f)
        except (json.JSONDecodeError, TypeError):
            pass

    if all_affected:
        await _git_add(sorted(all_affected), cwd=cwd)

    # Check if there's anything to commit
    if not await _has_staged_changes(cwd):
        logger.info("Hephaestus: no staged changes for project %s, skipping commit", project_id[:8])
        return [Emit("project_committed", {
            "project_id": project_id,
            "skipped": True,
            "reason": "no changes",
        }, source="hephaestus")]

    # Get task summary for commit message
    tasks = await db.fetchall(
        "SELECT title, status FROM tasks WHERE project_id = $1 ORDER BY wave, id",
        (project_id,),
    )
    task_lines = []
    for t in tasks:
        title = t.get("title", "?")
        status = t.get("status", "?")
        task_lines.append(f"- [{status}] {title}")
    task_summary = "\n".join(task_lines) if task_lines else "No tasks"

    # Create branch if on main
    current_branch = await _get_current_branch(cwd)
    branch_name = current_branch
    if current_branch in ("main", "master"):
        # Slugify project name for branch
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', project_name.lower()).strip('-')[:50]
        branch_name = f"hekate/{slug}"
        rc, _, err = await _git_run(["checkout", "-b", branch_name], cwd)
        if rc != 0:
            # Branch might already exist
            rc2, _, _ = await _git_run(["checkout", branch_name], cwd)
            if rc2 != 0:
                logger.error("Hephaestus: failed to create branch %s: %s", branch_name, err)
                branch_name = current_branch  # Fall back to current branch

    # Commit
    commit_msg = f"{project_name}\n\nAutonomous execution by Hekate gods pipeline.\n\n{task_summary}"
    rc, out, err = await _git_run(["commit", "-m", commit_msg], cwd)
    if rc != 0:
        logger.error("Hephaestus: git commit failed: %s", err)
        # Switch back to original branch if we created one
        if branch_name != current_branch:
            await _git_run(["checkout", current_branch], cwd)
        return [Emit("commit_failed", {
            "project_id": project_id,
            "error": err[:200],
        }, source="hephaestus")]

    # Extract commit SHA
    rc, sha, _ = await _git_run(["rev-parse", "HEAD"], cwd)
    commit_sha = sha[:12] if rc == 0 else "unknown"
    logger.info("Hephaestus: committed %s on branch %s for project %s",
                commit_sha, branch_name, project_id[:8])

    # Push
    rc, _, err = await _git_run(["push", "-u", "origin", branch_name], cwd, timeout=60.0)
    if rc != 0:
        logger.error("Hephaestus: git push failed: %s", err)
        return [Emit("project_committed", {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "branch": branch_name,
            "pushed": False,
            "error": err[:200],
        }, source="hephaestus")]

    # Create PR if we're on a feature branch
    pr_url = None
    if branch_name != current_branch and branch_name.startswith("hekate/"):
        import asyncio as _aio
        pr_proc = await _aio.create_subprocess_exec(
            "gh", "pr", "create",
            "--title", project_name,
            "--body", f"Autonomous execution by Hekate gods pipeline.\n\n{task_summary}",
            "--base", current_branch,
            "--head", branch_name,
            cwd=cwd,
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        try:
            pr_out, pr_err = await _aio.wait_for(pr_proc.communicate(), timeout=30.0)
            if pr_proc.returncode == 0:
                pr_url = (pr_out or b"").decode(errors="replace").strip()
                logger.info("Hephaestus: created PR %s for project %s", pr_url, project_id[:8])
            else:
                logger.warning("Hephaestus: gh pr create failed: %s",
                              (pr_err or b"").decode(errors="replace")[:200])
        except _aio.TimeoutError:
            logger.warning("Hephaestus: gh pr create timed out")

    emits = [Emit("project_committed", {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "branch": branch_name,
        "pushed": True,
        "pr_url": pr_url,
    }, source="hephaestus")]

    # Switch back to original branch
    if branch_name != current_branch:
        await _git_run(["checkout", current_branch], cwd)

    return emits
