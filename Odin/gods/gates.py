"""Gates — check functions that validate handler output.

Every gate takes (original_event, proposed_emits, db) and returns
GateResult(passed=bool, reason=str). If passed=False, the emits are
blocked and the handler can retry with the gate's feedback.

Gates answer: "Did this actually happen? Is it right?"
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gods.pipeline import Event, Emit, GateResult

logger = logging.getLogger("gods.gates")


# ---------------------------------------------------------------------------
# Planning gates
# ---------------------------------------------------------------------------

async def check_plan_created(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was a plan actually created and persisted?"""
    plan_emit = next((e for e in emits if e.event_type == "plan_generated"), None)
    if not plan_emit:
        return GateResult(False, "No plan_generated event emitted")

    plan_id = plan_emit.payload.get("plan_id")
    if not plan_id:
        return GateResult(False, "plan_generated event has no plan_id")

    # Verify plan exists in DB
    row = await db.fetchone(
        "SELECT id, plan_json FROM plans WHERE id = $1", (plan_id,)
    )
    if not row:
        return GateResult(False, f"Plan {plan_id} not found in database")

    # Verify plan JSON is parseable and has content
    plan_json = row["plan_json"] if isinstance(row, dict) else row[1]
    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except (json.JSONDecodeError, TypeError):
        return GateResult(False, "Plan JSON is not parseable")

    # Check it has tasks or phases or epics (depending on rigor)
    has_tasks = bool(plan.get("tasks"))
    has_phases = bool(plan.get("phases"))
    has_epics = bool(plan.get("epics"))
    if not (has_tasks or has_phases or has_epics):
        return GateResult(False, "Plan has no tasks, phases, or epics")

    return GateResult(True, "Plan created and persisted", {
        "plan_id": plan_id,
        "has_tasks": has_tasks,
        "has_phases": has_phases,
    })


async def check_plan_reviewed(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was the plan reviewed and does it pass quality checks?"""
    reviewed_emit = next((e for e in emits if e.event_type == "project_planned"), None)
    if not reviewed_emit:
        return GateResult(False, "No project_planned event emitted")

    plan_id = reviewed_emit.payload.get("plan_id")
    review = reviewed_emit.payload.get("review")

    if not review:
        # Allow through if review was explicitly skipped (review_cycle=False)
        if reviewed_emit.payload.get("review_skipped"):
            return GateResult(True, "Review skipped by configuration")
        return GateResult(False, "No review data in project_planned event")

    # Check the review verdict
    has_gaps = review.get("has_gaps", True)
    confidence = review.get("confidence", 0.0)

    if has_gaps and confidence < 0.7:
        gaps = review.get("gaps", [])
        return GateResult(False,
            f"Plan review found gaps (confidence={confidence}): {'; '.join(gaps[:3])}",
            {"gaps": gaps, "confidence": confidence},
        )

    return GateResult(True, f"Plan reviewed (confidence={confidence})", {
        "plan_id": plan_id,
        "confidence": confidence,
    })


# ---------------------------------------------------------------------------
# Execution gates
# ---------------------------------------------------------------------------

async def check_task_claimed(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was the task actually claimed and is it in RUNNING state?"""
    worker_emit = next((e for e in emits if e.event_type == "worker_event"), None)
    if not worker_emit:
        return GateResult(False, "No worker_event emitted")

    task_id = worker_emit.payload.get("task_id")
    if not task_id:
        return GateResult(False, "worker_event has no task_id")

    row = await db.fetchone(
        "SELECT status FROM tasks WHERE id = $1", (task_id,)
    )
    if not row:
        return GateResult(False, f"Task {task_id} not found")

    status = row["status"] if isinstance(row, dict) else row[0]
    if worker_emit.payload.get("status") == "started" and status != "running":
        return GateResult(False, f"Task {task_id} status is {status}, expected running")

    return GateResult(True, f"Task {task_id} claimed", {"task_id": task_id})


async def check_code_written(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was code actually produced? Is the output non-empty?"""
    worker_emit = next(
        (e for e in emits if e.event_type == "worker_event"
         and e.payload.get("status") == "completed"),
        None,
    )
    if not worker_emit:
        return GateResult(False, "No completed worker_event emitted")

    task_id = worker_emit.payload.get("task_id")
    output_len = worker_emit.payload.get("output_len", 0)

    if output_len == 0:
        return GateResult(False, f"Task {task_id} completed with empty output")

    # Verify output exists in DB
    row = await db.fetchone(
        "SELECT output_text FROM tasks WHERE id = $1", (task_id,)
    )
    if not row:
        return GateResult(False, f"Task {task_id} not found in database")

    output = row["output_text"] if isinstance(row, dict) else row[0]
    if not output or not output.strip():
        return GateResult(False, f"Task {task_id} has empty/whitespace-only output in DB")

    return GateResult(True, f"Task {task_id} produced output ({output_len} chars)", {
        "task_id": task_id,
        "output_len": output_len,
    })


async def check_code_parses(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Does the produced code parse (Python syntax check)?"""
    worker_emit = next(
        (e for e in emits if e.event_type == "worker_event"
         and e.payload.get("status") == "completed"),
        None,
    )
    if not worker_emit:
        return GateResult(False, "No completed worker_event")

    files_changed = worker_emit.payload.get("files_changed", [])
    py_files = [f for f in files_changed if f.endswith(".py")]

    if not py_files:
        return GateResult(True, "No Python files changed, skipping syntax check")

    # Syntax check is done by the handler itself — we just verify it reported clean
    syntax_ok = worker_emit.payload.get("syntax_check_passed", True)
    if not syntax_ok:
        errors = worker_emit.payload.get("syntax_errors", [])
        return GateResult(False,
            f"Syntax errors in {len(errors)} file(s): {'; '.join(errors[:3])}",
            {"syntax_errors": errors},
        )

    return GateResult(True, f"Syntax check passed ({len(py_files)} .py files)")


# ---------------------------------------------------------------------------
# Verification gates
# ---------------------------------------------------------------------------

async def check_verification_ran(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Did verification actually run and produce a verdict?"""
    verified_emit = next(
        (e for e in emits if e.event_type in ("task_verified", "task_rejected")),
        None,
    )
    if not verified_emit:
        return GateResult(False, "No task_verified or task_rejected event emitted")

    verdict = verified_emit.payload.get("verdict")
    if not verdict:
        return GateResult(False, "Verification event has no verdict")

    task_id = verified_emit.payload.get("task_id")
    return GateResult(True, f"Verification ran: {verdict}", {
        "task_id": task_id,
        "verdict": verdict,
    })


async def check_tests_pass(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Do the tests pass? (TDD verification)"""
    worker_emit = next(
        (e for e in emits if e.event_type == "worker_event"
         and e.payload.get("status") == "completed"),
        None,
    )
    if not worker_emit:
        return GateResult(False, "No completed worker_event")

    test_result = worker_emit.payload.get("test_result")
    if test_result is None:
        return GateResult(True, "No test results reported, skipping")

    if not test_result.get("passed", False):
        failures = test_result.get("failures", [])
        return GateResult(False,
            f"Tests failed: {len(failures)} failure(s)",
            {"test_failures": failures[:5]},
        )

    return GateResult(True, f"Tests passed ({test_result.get('total', '?')} tests)", {
        "tests_passed": test_result.get("passed_count", 0),
        "tests_total": test_result.get("total", 0),
    })


# ---------------------------------------------------------------------------
# Git gates
# ---------------------------------------------------------------------------

async def check_files_staged(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Were files actually staged in git?"""
    # hephaestus_stage emits "files_staged" or "stage_failed"
    failed_emit = next((e for e in emits if e.event_type == "stage_failed"), None)
    if failed_emit:
        return GateResult(False, f"Staging failed: {failed_emit.payload.get('error', 'unknown')}")

    git_emit = next((e for e in emits if e.event_type == "files_staged"), None)
    if not git_emit:
        # Handler returned None (no affected_files) — nothing to stage is OK
        return GateResult(True, "No files to stage")

    files = git_emit.payload.get("files", [])
    if not files:
        return GateResult(False, "files_staged event has empty file list")

    return GateResult(True, f"Staged {len(files)} file(s)", {"files": files})


async def check_pr_created(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was the project committed (and optionally PR created)?"""
    # hephaestus_complete emits "project_committed" or "commit_failed"
    failed_emit = next((e for e in emits if e.event_type == "commit_failed"), None)
    if failed_emit:
        return GateResult(False, f"Commit failed: {failed_emit.payload.get('error', 'unknown')}")

    pr_emit = next((e for e in emits if e.event_type == "project_committed"), None)
    if not pr_emit:
        return GateResult(False, "No project_committed event emitted")

    # Skipped (no changes) is a valid outcome
    if pr_emit.payload.get("skipped"):
        return GateResult(True, "No changes to commit", {"skipped": True})

    # Pushed without PR is valid (e.g., already on feature branch)
    pr_url = pr_emit.payload.get("pr_url")
    pushed = pr_emit.payload.get("pushed", False)
    commit_sha = pr_emit.payload.get("commit_sha", "")

    if pr_url:
        return GateResult(True, f"PR created: {pr_url}", {"pr_url": pr_url})
    if pushed:
        return GateResult(True, f"Committed and pushed ({commit_sha})", {"commit_sha": commit_sha})

    return GateResult(True, f"Committed locally ({commit_sha})", {"commit_sha": commit_sha})


# ---------------------------------------------------------------------------
# TDD gates — enforce test-first development
# ---------------------------------------------------------------------------

async def check_test_written_first(
    event: Event, emits: list[Emit], db
) -> GateResult:
    """Gate: Was a failing test written before implementation?

    Checks that the worker_event includes test_written=True in its payload.
    The executor is responsible for reporting this from the TDD flow.
    """
    worker_emit = next(
        (e for e in emits if e.event_type == "worker_event"),
        None,
    )
    if not worker_emit:
        return GateResult(False, "No worker_event emitted")

    tdd_phase = worker_emit.payload.get("tdd_phase")
    if tdd_phase == "test_written":
        test_fails = worker_emit.payload.get("test_fails", False)
        if not test_fails:
            return GateResult(False, "Test was written but doesn't fail — not a valid red phase")
        return GateResult(True, "Failing test written (red phase)")

    if tdd_phase == "implementation":
        test_passes = worker_emit.payload.get("test_passes", False)
        if not test_passes:
            return GateResult(False, "Implementation written but test still fails — not green yet")
        return GateResult(True, "Implementation makes test pass (green phase)")

    if tdd_phase == "refactor":
        test_passes = worker_emit.payload.get("test_passes", False)
        if not test_passes:
            return GateResult(False, "Refactor broke the test")
        return GateResult(True, "Refactor complete, test still passes")

    # No TDD phase reported — skip TDD gate
    return GateResult(True, "No TDD phase reported, skipping")


# ---------------------------------------------------------------------------
# Composite gates — combine multiple checks
# ---------------------------------------------------------------------------

def compose_gates(*gates: Gate) -> Gate:
    """Combine multiple gates — all must pass."""
    async def composed(event, emits, db):
        for gate in gates:
            result = await gate(event, emits, db)
            if not result.passed:
                return result
        return GateResult(True, "All gates passed")
    composed.__name__ = f"compose({', '.join(g.__name__ for g in gates)})"
    return composed
