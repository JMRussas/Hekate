#!/usr/bin/env python
"""Orchestration supervisor — monitors tasks, detects failures, auto-fixes.

Catches both explicit failures (status=failed) and silent failures
(status=completed but output indicates no code was written). Diagnoses
root causes and takes corrective action: re-queue on different tier,
fix prompts, escalate to human.

Usage:
    python orchestration/tools/supervisor.py                # Run once
    python orchestration/tools/supervisor.py --loop         # Poll continuously
    python orchestration/tools/supervisor.py --audit        # Audit all completed tasks
    python orchestration/tools/supervisor.py --fix          # Audit + auto-fix

Depends on: orchestration/data/orchestration.db
Used by:    manual or cron on 4090
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "orchestration.db"

# ---------------------------------------------------------------------------
# Failure detection patterns
# ---------------------------------------------------------------------------

# Phrases that indicate the task didn't actually produce code
SILENT_FAILURE_PATTERNS = [
    # Permission / sandbox blocked
    r"(?i)workspace is read[- ]only",
    r"(?i)I (?:am |was )?unable to (?:directly )?(?:create|write|modify|edit) files?",
    r"(?i)cannot write",
    r"(?i)file (?:write |editing )?permissions? need",
    r"(?i)could you (?:approve|grant)",
    r"(?i)sandbox.*read[- ]?only",
    r"(?i)rejected by policy",
    r"(?i)blocked.*permission",
    # Agent/tool failures
    r"(?i)generalist agent (?:is failing|encountered|seems to have)",
    r"(?i)(?:I am |I'm )?fundamentally blocked",
    r"(?i)tools? (?:are |is )?(?:not )?(?:available|functional|broken|unavailable)",
    r"(?i)cannot (?:proceed|complete|execute)",
    r"(?i)I (?:am |was )?blocked",
    # Gave up / produced description instead of code
    r"(?i)what I would (?:change|do|implement|write)",
    r"(?i)once (?:writable|I have access|you grant)",
    r"(?i)if you want this implemented",
    r"(?i)point me at the actual",
    r"(?i)the implementation below is the concrete content I would put",
    # Nested session errors
    r"(?i)cannot be launched inside another.*session",
    r"(?i)unset the CLAUDECODE",
]

# Phrases that indicate a real error (not just a planning output)
EXPLICIT_FAILURE_PATTERNS = [
    r"(?i)\[CLI failed",
    r"(?i)\[CLI timed out",
    r"(?i)\[Ollama error",
    r"(?i)error:.*model.*(?:not found|invalid|does not exist)",
]

# Known root causes and their fixes
DIAGNOSIS_RULES = [
    {
        "name": "read_only_sandbox",
        "pattern": r"(?i)(?:read[- ]only|rejected by policy|sandbox)",
        "cause": "CLI running without write permissions",
        "fix": "reassign_tier",  # Move to a tier with write access
        "target_tier": "claude_code",
    },
    {
        "name": "no_file_tools",
        "pattern": r"(?i)(?:unable to.*(?:create|write) files|generalist.*failing|tools.*unavailable)",
        "cause": "CLI agent lacks file creation tools",
        "fix": "reassign_tier",
        "target_tier": "claude_code",
    },
    {
        "name": "nested_session",
        "pattern": r"(?i)cannot be launched inside another.*session|unset.*CLAUDECODE",
        "cause": "Env vars leaking from parent Claude session",
        "fix": "retry_same",  # Just retry — executor should strip env
    },
    {
        "name": "invalid_model",
        "pattern": r"(?i)model.*(?:not found|invalid|does not exist)",
        "cause": "Model ID doesn't exist",
        "fix": "reassign_tier",
        "target_tier": "claude_code",
    },
    {
        "name": "timeout",
        "pattern": r"(?i)timed out after",
        "cause": "Task exceeded execution timeout",
        "fix": "retry_same",  # Retry once, then reassign
    },
    {
        "name": "wrong_repo",
        "pattern": r"(?i)(?:file.*does not exist|not present|not found in this repo)",
        "cause": "Task references files in a different repo",
        "fix": "mark_invalid",
    },
    {
        "name": "description_only",
        "pattern": r"(?i)what I would (?:change|do|implement)|once (?:writable|I have access)",
        "cause": "Agent produced a description instead of code",
        "fix": "reassign_tier",
        "target_tier": "claude_code",
    },
]

# Tiers ordered by capability (fallback chain)
TIER_FALLBACK = ["claude_code", "gemini_cli", "codex_cli", "ollama"]


def get_db():
    """Open the orchestration DB."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_silent_failure(output_text):
    """Check if a 'completed' task actually failed silently."""
    if not output_text:
        return True, "empty_output", "Task completed with no output"

    matches = []
    for pattern in SILENT_FAILURE_PATTERNS:
        if re.search(pattern, output_text):
            matches.append(pattern)

    # Check for success signals that override failure patterns
    has_tests_pass = bool(re.search(r"(?:all )?\d+ tests? pass", output_text, re.I))
    has_build_ok = bool(re.search(r"build (?:succeed|pass|clean|0 error)", output_text, re.I))
    has_done = bool(re.search(r"(?:^|\n)(?:done|complete|implemented)\b.*(?:pass|succeed|clean)", output_text, re.I))
    success_signals = sum([has_tests_pass, has_build_ok, has_done])

    if len(matches) >= 2 and success_signals == 0:
        # Multiple failure indicators, no success signals = high confidence failure
        return True, "multiple_failure_signals", f"Matched {len(matches)} failure patterns, no success signals"
    elif len(matches) >= 1 and success_signals == 0:
        # Single failure match, no success — check for real code output
        has_code = bool(re.search(r"```(?:csharp|python|typescript|json)\n.{100,}", output_text, re.DOTALL))
        if not has_code:
            return True, "failure_signal_no_code", f"Failure pattern found, no code blocks, no success signals"

    return False, None, None


def detect_explicit_failure(error_text):
    """Check error field for known failure patterns."""
    if not error_text:
        return None
    for pattern in EXPLICIT_FAILURE_PATTERNS:
        if re.search(pattern, error_text):
            return error_text[:200]
    return error_text[:200]


def diagnose(output_text, error_text, current_tier):
    """Diagnose root cause and recommend a fix."""
    text = f"{output_text or ''}\n{error_text or ''}"

    for rule in DIAGNOSIS_RULES:
        if re.search(rule["pattern"], text):
            fix = rule["fix"]
            target = rule.get("target_tier", current_tier)

            # Don't reassign to the same tier that already failed
            if fix == "reassign_tier" and target == current_tier:
                # Walk the fallback chain
                idx = TIER_FALLBACK.index(current_tier) if current_tier in TIER_FALLBACK else -1
                for fallback in TIER_FALLBACK:
                    if fallback != current_tier:
                        target = fallback
                        break

            return {
                "rule": rule["name"],
                "cause": rule["cause"],
                "fix": fix,
                "target_tier": target,
            }

    return {
        "rule": "unknown",
        "cause": "Unrecognized failure",
        "fix": "escalate",
        "target_tier": current_tier,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def requeue_task(db, task_id, new_tier=None, reason=""):
    """Reset a task to pending, optionally changing tier."""
    now = time.time()
    if new_tier:
        db.execute(
            "UPDATE tasks SET status = 'pending', model_tier = ?, error = NULL, "
            "output_text = NULL, started_at = NULL, completed_at = NULL, "
            "claimed_by = NULL, claimed_at = NULL, retry_count = 0, "
            "updated_at = ? WHERE id = ?",
            (new_tier, now, task_id),
        )
    else:
        db.execute(
            "UPDATE tasks SET status = 'pending', error = NULL, "
            "output_text = NULL, started_at = NULL, completed_at = NULL, "
            "claimed_by = NULL, claimed_at = NULL, "
            "updated_at = ? WHERE id = ?",
            (now, task_id),
        )
    db.commit()


def mark_needs_review(db, task_id, reason):
    """Flag task for human review."""
    now = time.time()
    db.execute(
        "UPDATE tasks SET status = 'needs_review', "
        "verification_status = 'failed', verification_notes = ?, "
        "updated_at = ? WHERE id = ?",
        (reason[:500], now, task_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit_task(task, verbose=False):
    """Audit a single task. Returns diagnosis dict or None if healthy."""
    task_id = task["id"]
    title = task["title"]
    status = task["status"]
    tier = task["model_tier"]
    output = task["output_text"] or ""
    error = task["error"] or ""

    issues = []

    # Check explicit failures
    if status == "failed":
        err_summary = detect_explicit_failure(error)
        diag = diagnose(output, error, tier)
        issues.append({
            "type": "explicit_failure",
            "detail": err_summary,
            **diag,
        })

    # Check silent failures (completed but no real code)
    elif status == "completed":
        is_silent, signal, detail = detect_silent_failure(output)
        if is_silent:
            diag = diagnose(output, error, tier)
            issues.append({
                "type": "silent_failure",
                "signal": signal,
                "detail": detail,
                **diag,
            })

    # Check stale running tasks (running > 15 minutes with no progress)
    elif status == "running":
        started = task["started_at"] or 0
        if time.time() - started > 900:  # 15 min
            issues.append({
                "type": "stale_running",
                "detail": f"Running for {int((time.time() - started) / 60)}m",
                "rule": "timeout",
                "cause": "Task running too long",
                "fix": "retry_same",
                "target_tier": tier,
            })

    if issues:
        return {
            "task_id": task_id,
            "title": title,
            "tier": tier,
            "status": status,
            "issues": issues,
        }
    return None


def audit_all(db, verbose=False):
    """Audit all tasks, return list of problems."""
    problems = []
    for task in db.execute(
        "SELECT * FROM tasks ORDER BY wave, priority"
    ).fetchall():
        result = audit_task(task, verbose)
        if result:
            problems.append(result)
    return problems


def apply_fixes(db, problems, dry_run=False):
    """Apply recommended fixes for detected problems."""
    actions = []
    for problem in problems:
        task_id = problem["task_id"]
        title = problem["title"]
        current_tier = problem["tier"]

        for issue in problem["issues"]:
            fix = issue["fix"]
            target = issue.get("target_tier", current_tier)
            cause = issue["cause"]

            if fix == "reassign_tier":
                action = f"REQUEUE {task_id[:8]} -> {target} (was {current_tier}): {cause}"
                actions.append(action)
                if not dry_run:
                    requeue_task(db, task_id, new_tier=target, reason=cause)

            elif fix == "retry_same":
                retry_count = db.execute(
                    "SELECT retry_count, max_retries FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if retry_count and retry_count["retry_count"] < retry_count["max_retries"]:
                    action = f"RETRY   {task_id[:8]} on {current_tier} (attempt {retry_count['retry_count'] + 1}): {cause}"
                    actions.append(action)
                    if not dry_run:
                        requeue_task(db, task_id, reason=cause)
                        db.execute(
                            "UPDATE tasks SET retry_count = retry_count + 1 WHERE id = ?",
                            (task_id,),
                        )
                        db.commit()
                else:
                    # Exhausted retries — reassign to claude_code
                    action = f"REQUEUE {task_id[:8]} -> claude_code (retries exhausted): {cause}"
                    actions.append(action)
                    if not dry_run:
                        requeue_task(db, task_id, new_tier="claude_code", reason=cause)

            elif fix == "mark_invalid":
                action = f"INVALID {task_id[:8]}: {cause}"
                actions.append(action)
                if not dry_run:
                    mark_needs_review(db, task_id, cause)

            elif fix == "escalate":
                action = f"ESCALATE {task_id[:8]}: {cause} — needs human review"
                actions.append(action)
                if not dry_run:
                    mark_needs_review(db, task_id, cause)

    return actions


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_report(problems, actions=None):
    """Print a human-readable report."""
    if not problems:
        print("All tasks healthy.")
        return

    print(f"\n{'=' * 70}")
    print(f"  SUPERVISOR REPORT — {len(problems)} issues found")
    print(f"{'=' * 70}\n")

    by_type = {}
    for p in problems:
        for issue in p["issues"]:
            t = issue["type"]
            by_type.setdefault(t, []).append(p)

    for issue_type, tasks in by_type.items():
        label = {
            "explicit_failure": "EXPLICIT FAILURES",
            "silent_failure": "SILENT FAILURES (completed but no code)",
            "stale_running": "STALE RUNNING TASKS",
        }.get(issue_type, issue_type.upper())

        print(f"--- {label} ({len(tasks)}) ---")
        for p in tasks:
            issue = [i for i in p["issues"] if i["type"] == issue_type][0]
            print(f"  {p['task_id'][:8]} | {p['tier']:12} | {p['title']}")
            print(f"           Cause: {issue['cause']}")
            print(f"           Fix:   {issue['fix']} -> {issue.get('target_tier', '?')}")
        print()

    if actions:
        print(f"--- ACTIONS {'(DRY RUN)' if '[DRY' in str(actions) else 'APPLIED'} ---")
        for a in actions:
            print(f"  {a}")
        print()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(db, auto_fix=False, dry_run=False):
    """Run one audit cycle. Returns number of issues found."""
    problems = audit_all(db)

    if auto_fix and problems:
        actions = apply_fixes(db, problems, dry_run=dry_run)
        print_report(problems, actions)
        return len(problems)
    else:
        print_report(problems)
        return len(problems)


def main():
    parser = argparse.ArgumentParser(description="Orchestration supervisor")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval (seconds)")
    parser.add_argument("--audit", action="store_true", help="Audit all tasks")
    parser.add_argument("--fix", action="store_true", help="Audit + auto-fix")
    parser.add_argument("--dry-run", action="store_true", help="Show fixes without applying")
    args = parser.parse_args()

    db = get_db()

    if args.audit or args.fix:
        run_once(db, auto_fix=args.fix, dry_run=args.dry_run)
    elif args.loop:
        print(f"Supervisor polling every {args.interval}s...")
        try:
            while True:
                count = run_once(db, auto_fix=True)
                if count == 0:
                    time.sleep(args.interval)
                else:
                    time.sleep(5)  # Quick re-check after fixes
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once(db)

    db.close()


if __name__ == "__main__":
    main()
