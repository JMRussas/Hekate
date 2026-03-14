#!/usr/bin/env python
"""Local task executor — claims and runs orchestration tasks directly.

Polls the orchestration DB for claimable tasks, executes them via the
appropriate CLI (claude, gemini, codex) or Ollama, and writes results
back. No REST API or auth needed — direct SQLite access.

Designed for the 4090 dev machine which has all 3 CLIs + Ollama.

Usage:
    python orchestration/tools/local_executor.py                # Run once (claim + execute one task)
    python orchestration/tools/local_executor.py --loop          # Poll continuously
    python orchestration/tools/local_executor.py --loop --interval 30  # Poll every 30s
    python orchestration/tools/local_executor.py --dry-run       # Show what would be claimed
    python orchestration/tools/local_executor.py --tier claude_code  # Only claim claude_code tasks
    python orchestration/tools/local_executor.py --status        # Show current task status

Depends on: orchestration/data/orchestration.db
Used by:    manual execution from 4090
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "orchestration.db"

# CLI timeout (seconds)
CLI_TIMEOUT = 600

# Default models per tier
DEFAULT_MODELS = {
    "claude_code": None,  # Claude Code uses its own model selection
    "gemini_cli": "gemini-2.5-pro",
    "codex_cli": "gpt-5.4",
    "ollama": "qwen2.5-coder:14b",
}

# Tiers this executor can handle
SUPPORTED_TIERS = {"claude_code", "gemini_cli", "codex_cli", "ollama"}

# Machine identifier for claimed_by
MACHINE_ID = "4090-local"


def resolve_cmd(name):
    """Resolve a CLI command to full path (handles .cmd on Windows)."""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == "win32":
        npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm")
        for ext in (".cmd", ".exe", ""):
            candidate = os.path.join(npm_bin, f"{name}{ext}")
            if os.path.isfile(candidate):
                return candidate
    return name


def get_db():
    """Open the orchestration DB."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_project_cwd(db, project_id):
    """Look up the project's repo_path."""
    row = db.execute(
        "SELECT repo_path FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row and row["repo_path"]:
        return row["repo_path"]
    return None


def find_claimable_task(db, tier_filter=None):
    """Find the next claimable task: PENDING, current wave, dependencies met."""
    # Find current wave (lowest wave with incomplete tasks)
    row = db.execute(
        "SELECT MIN(wave) as current_wave FROM tasks "
        "WHERE status NOT IN ('completed', 'failed', 'cancelled', 'needs_review')"
    ).fetchone()
    if not row or row["current_wave"] is None:
        return None
    current_wave = row["current_wave"]

    # Get pending tasks in current wave, ordered by priority
    query = (
        "SELECT * FROM tasks WHERE wave = ? AND status = 'pending' "
        "ORDER BY priority ASC"
    )
    candidates = db.execute(query, (current_wave,)).fetchall()

    for task in candidates:
        # Filter by tier if requested
        if tier_filter and task["model_tier"] != tier_filter:
            continue
        # Only handle supported tiers
        if task["model_tier"] not in SUPPORTED_TIERS:
            continue
        # Check dependencies are met
        req_ids = json.loads(task["requirement_ids_json"] or "[]")
        if req_ids:
            placeholders = ",".join("?" for _ in req_ids)
            completed = db.execute(
                f"SELECT COUNT(*) as cnt FROM tasks WHERE id IN ({placeholders}) "
                f"AND status = 'completed'",
                req_ids,
            ).fetchone()["cnt"]
            if completed < len(req_ids):
                continue  # Dependencies not met
        return task
    return None


def claim_task(db, task_id):
    """Atomically claim a task (CAS: pending -> running)."""
    now = time.time()
    cursor = db.execute(
        "UPDATE tasks SET status = 'running', claimed_by = ?, claimed_at = ?, "
        "started_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
        (MACHINE_ID, now, now, now, task_id),
    )
    db.commit()
    return cursor.rowcount > 0


def build_prompt(task):
    """Build the full prompt from task description + context."""
    parts = []
    system_prompt = task["system_prompt"] or ""
    if system_prompt:
        parts.append(system_prompt)

    context_json = task["context_json"] or "[]"
    context = json.loads(context_json) if isinstance(context_json, str) else context_json
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        content = ctx.get("content", "")
        if content:
            parts.append(f"<{ctx_type}>\n{content}\n</{ctx_type}>")

    parts.append(task["description"])
    return "\n\n".join(parts)


def execute_claude(prompt, cwd):
    """Execute via Claude Code CLI."""
    cmd = resolve_cmd("claude")
    args = [
        cmd, "-p",
        "--verbose",
        "--output-format", "stream-json",
        "--allowedTools",
        "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(dotnet *),Bash(npm *),Bash(python *)",
    ]
    # Strip nested session env vars (CLAUDECODE and CLAUDE_* both trigger detection)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") }
    return _run_cli(args, prompt, cwd, env=env)


def execute_gemini(prompt, cwd):
    """Execute via Gemini CLI."""
    cmd = resolve_cmd("gemini")
    model = DEFAULT_MODELS["gemini_cli"]
    args = [cmd, "-p", ""]
    if model:
        args.extend(["-m", model])
    return _run_cli(args, prompt, cwd)


def execute_codex(prompt, cwd):
    """Execute via Codex CLI."""
    cmd = resolve_cmd("codex")
    model = DEFAULT_MODELS["codex_cli"]
    args = [cmd, "exec", "--full-auto"]
    if model:
        args.extend(["--model", model])
    return _run_cli(args, prompt, cwd)


def execute_ollama(prompt, cwd):
    """Execute via Ollama HTTP API (local)."""
    import urllib.request
    model = DEFAULT_MODELS["ollama"]
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=CLI_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", ""), model
    except Exception as e:
        return f"[Ollama error: {e}]", model


def _run_cli(args, prompt, cwd, env=None):
    """Run a CLI subprocess with prompt on stdin."""
    try:
        proc = subprocess.run(
            args,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=CLI_TIMEOUT,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"[CLI timed out after {CLI_TIMEOUT}s]", "unknown"

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0 and not stdout:
        return f"[CLI failed (exit {proc.returncode}): {stderr[:500]}]", "unknown"

    # For Claude Code stream-json, extract text parts
    if "--output-format" in " ".join(args) and "stream-json" in " ".join(args):
        stdout = _extract_claude_text(stdout)

    return stdout, args[0].split(os.sep)[-1]


def _extract_claude_text(raw_output):
    """Extract text content from Claude Code stream-json output."""
    text_parts = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta["text"])
            elif event.get("type") == "result":
                # Final result object
                result_text = event.get("result", "")
                if result_text and not text_parts:
                    text_parts.append(result_text)
        except json.JSONDecodeError:
            continue
    return "".join(text_parts) if text_parts else raw_output


def complete_task(db, task_id, output, model_used):
    """Write task result back to DB."""
    now = time.time()
    db.execute(
        "UPDATE tasks SET status = 'completed', output_text = ?, model_used = ?, "
        "completed_at = ?, updated_at = ? WHERE id = ?",
        (output, model_used, now, now, task_id),
    )
    db.commit()


def fail_task(db, task_id, error_msg):
    """Mark task as failed."""
    now = time.time()
    db.execute(
        "UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
        (error_msg[:2000], now, task_id),
    )
    db.commit()


def show_status(db):
    """Print current task status summary."""
    print("=" * 70)
    print("ORCHESTRATION STATUS")
    print("=" * 70)

    # Current wave
    row = db.execute(
        "SELECT MIN(wave) as current_wave FROM tasks "
        "WHERE status NOT IN ('completed', 'failed', 'cancelled', 'needs_review')"
    ).fetchone()
    current_wave = row["current_wave"] if row else "N/A"
    print(f"\nCurrent wave: {current_wave}")

    # By status
    print("\n--- By Status ---")
    for r in db.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status ORDER BY status"
    ):
        print(f"  {r['status']:12} {r['cnt']}")

    # By tier + status
    print("\n--- By Tier ---")
    for r in db.execute(
        "SELECT model_tier, status, COUNT(*) as cnt FROM tasks "
        "GROUP BY model_tier, status ORDER BY model_tier, status"
    ):
        print(f"  {r['model_tier']:12} | {r['status']:12} | {r['cnt']}")

    # Running tasks
    print("\n--- Running ---")
    for r in db.execute("SELECT id, title, model_tier, claimed_by FROM tasks WHERE status = 'running'"):
        print(f"  {r['id']} | {r['model_tier']:12} | {r['claimed_by'] or '?':15} | {r['title']}")

    # Claimable
    print("\n--- Claimable ---")
    task = find_claimable_task(db)
    if task:
        print(f"  Next: {task['id']} | w{task['wave']} p{task['priority']} | {task['model_tier']} | {task['title']}")
    else:
        print("  None (all tasks claimed, blocked, or completed)")


def get_current_wave(db):
    """Get the current (lowest incomplete) wave number."""
    row = db.execute(
        "SELECT MIN(wave) as current_wave FROM tasks "
        "WHERE status NOT IN ('completed', 'failed', 'cancelled', 'needs_review')"
    ).fetchone()
    return row["current_wave"] if row else None


def is_wave_complete(db, wave):
    """Check if all tasks in a wave are completed."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM tasks WHERE wave = ? "
        "AND status NOT IN ('completed', 'failed', 'cancelled')",
        (wave,),
    ).fetchone()
    return row["cnt"] == 0


def run_wave_review(db, completed_wave, cwd):
    """Multi-model review when a wave completes.

    Sends the git diff from the wave's work to claude, gemini, and codex.
    Each reviews independently, then findings are reconciled.
    """
    print(f"\n{'=' * 60}")
    print(f"  WAVE {completed_wave} REVIEW — multi-model consensus")
    print(f"{'=' * 60}")

    # Get git diff for changes made during this wave
    try:
        diff_result = subprocess.run(
            ["git", "diff", "HEAD~5", "--stat"],
            capture_output=True, timeout=30, cwd=cwd,
            text=True,
        )
        diff_stat = diff_result.stdout.strip()

        full_diff = subprocess.run(
            ["git", "diff", "HEAD~5"],
            capture_output=True, timeout=30, cwd=cwd,
            text=True,
        )
        diff_text = full_diff.stdout[:15000]  # Cap at 15K chars
    except Exception as e:
        print(f"  Could not get git diff: {e}")
        diff_stat = "unavailable"
        diff_text = ""

    if not diff_text:
        print("  No diff to review. Skipping.")
        return None

    # Get completed task titles for context
    tasks = db.execute(
        "SELECT title, model_tier, model_used FROM tasks "
        "WHERE wave = ? AND status = 'completed'",
        (completed_wave,),
    ).fetchall()
    task_summary = "\n".join(
        f"  - {t['title']} (executed by {t['model_used'] or t['model_tier']})"
        for t in tasks
    )

    review_prompt = f"""Review the following code changes from Wave {completed_wave} of an orchestrated development plan.

<tasks_completed>
{task_summary}
</tasks_completed>

<diff_summary>
{diff_stat}
</diff_summary>

<diff>
{diff_text}
</diff>

Review for:
1. **Correctness**: Logic bugs, off-by-one, null refs, operator precedence
2. **Consistency**: Naming conventions, patterns match existing code
3. **Integration**: Do the pieces from different tasks fit together correctly
4. **Missing pieces**: Anything obviously missing that should have been included

Output a structured review:
- CRITICAL: Must fix before proceeding (blocks next wave)
- WARNING: Should fix but not blocking
- INFO: Observations, suggestions

Be specific. Reference file names and line numbers where possible."""

    # Fan out to available models
    # Claude Code gets Hecate MCP tools for deep analysis
    hecate_review_prompt = f"""Review the following code changes from Wave {completed_wave} of an orchestrated development plan.

<tasks_completed>
{task_summary}
</tasks_completed>

<diff_summary>
{diff_stat}
</diff_summary>

You have access to the Hecate MCP server. Use these tools to do a thorough review:
1. Call `review` on each modified file to check role constraints and guidelines
2. Call `verify` on modified C# files to compile-check them
3. Call `check_contracts` on any new or modified types
4. Call `check_allocations` on files in hot-path roles

After running the tools, synthesize a review:
- CRITICAL: Must fix before proceeding (blocks next wave)
- WARNING: Should fix but not blocking
- INFO: Observations, suggestions

Be specific. Reference file names and line numbers."""

    reviews = {}
    reviewers = [
        ("claude", execute_claude, hecate_review_prompt),  # Has Hecate MCP
        ("gemini", execute_gemini, review_prompt),
        ("codex", execute_codex, review_prompt),
    ]

    for name, executor, prompt in reviewers:
        print(f"  Sending to {name}...")
        try:
            output, model = executor(prompt, cwd)
            if not output.startswith("["):
                reviews[name] = output
                print(f"  {name}: received ({len(output)} chars)")
            else:
                print(f"  {name}: failed — {output[:100]}")
        except Exception as e:
            print(f"  {name}: error — {e}")

    if not reviews:
        print("  No reviews received. Skipping consensus.")
        return None

    # Reconcile if 2+ reviews
    if len(reviews) >= 2:
        print(f"\n  Reconciling {len(reviews)} reviews...")
        reconcile_prompt = f"""You are reconciling code reviews from {len(reviews)} different AI models.

"""
        for name, review in reviews.items():
            reconcile_prompt += f"<review model=\"{name}\">\n{review}\n</review>\n\n"

        reconcile_prompt += """Reconcile these reviews:
1. Items flagged by 2+ reviewers -> HIGH CONFIDENCE
2. Items flagged by only 1 reviewer -> MEDIUM CONFIDENCE
3. Contradictions between reviewers -> FLAG FOR HUMAN REVIEW

Output a single consolidated review with confidence levels."""

        consensus, _ = execute_ollama(reconcile_prompt, cwd)
        reviews["consensus"] = consensus
    else:
        # Single review — use it as-is
        reviews["consensus"] = list(reviews.values())[0]

    # Store review results
    review_text = f"=== Wave {completed_wave} Multi-Model Review ===\n\n"
    for name, review in reviews.items():
        review_text += f"--- {name.upper()} ---\n{review}\n\n"

    print(f"\n  Review complete. {len(reviews)} perspectives.")

    # Check for CRITICAL findings
    consensus = reviews.get("consensus", "")
    has_critical = "CRITICAL" in consensus.upper() and any(
        word in consensus.upper()
        for word in ["MUST FIX", "BLOCKS", "BUG", "INCORRECT", "WRONG"]
    )

    if has_critical:
        print("  *** CRITICAL issues found — next wave should address these ***")

    return review_text


def run_one(db, tier_filter=None, dry_run=False, last_wave=[None]):
    """Find, claim, and execute one task. Returns True if work was done."""
    current_wave = get_current_wave(db)

    # Detect wave transition — run review when a wave just completed
    if last_wave[0] is not None and current_wave is not None and current_wave > last_wave[0]:
        completed_wave = last_wave[0]
        # Get cwd from any task in the completed wave
        any_task = db.execute(
            "SELECT project_id FROM tasks WHERE wave = ? LIMIT 1",
            (completed_wave,),
        ).fetchone()
        if any_task:
            cwd = get_project_cwd(db, any_task["project_id"])
            review = run_wave_review(db, completed_wave, cwd)
            if review:
                # Store review in a log file
                log_dir = DB_PATH.parent / "reviews"
                log_dir.mkdir(exist_ok=True)
                log_file = log_dir / f"wave_{completed_wave}_review.txt"
                log_file.write_text(review, encoding="utf-8")
                print(f"  Review saved to {log_file}")

    last_wave[0] = current_wave

    task = find_claimable_task(db, tier_filter)
    if not task:
        return False

    tier = task["model_tier"]
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Claiming: {task['title']}")
    print(f"  ID: {task['id']} | Wave: {task['wave']} | Tier: {tier}")

    if dry_run:
        print(f"  Description: {task['description'][:200]}...")
        return True

    if not claim_task(db, task["id"]):
        print("  FAILED to claim (race condition — another executor got it)")
        return True  # Return True to keep polling

    print(f"  Claimed. Executing via {tier}...")

    cwd = get_project_cwd(db, task["project_id"])
    prompt = build_prompt(task)

    try:
        if tier == "claude_code":
            output, model_used = execute_claude(prompt, cwd)
        elif tier == "gemini_cli":
            output, model_used = execute_gemini(prompt, cwd)
        elif tier == "codex_cli":
            output, model_used = execute_codex(prompt, cwd)
        elif tier == "ollama":
            output, model_used = execute_ollama(prompt, cwd)
        else:
            fail_task(db, task["id"], f"Unsupported tier: {tier}")
            return True

        if output.startswith("[") and ("error" in output.lower() or "failed" in output.lower() or "timed out" in output.lower()):
            print(f"  FAILED: {output[:200]}")
            fail_task(db, task["id"], output)
        else:
            preview = output[:200] + "..." if len(output) > 200 else output
            print(f"  DONE: {preview}")
            complete_task(db, task["id"], output, model_used)

    except Exception as e:
        print(f"  ERROR: {e}")
        fail_task(db, task["id"], str(e))

    return True


def main():
    parser = argparse.ArgumentParser(description="Local task executor")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--interval", type=int, default=15, help="Poll interval (seconds)")
    parser.add_argument("--tier", type=str, help="Only claim tasks of this tier")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be claimed")
    parser.add_argument("--status", action="store_true", help="Show current status")
    args = parser.parse_args()

    db = get_db()

    if args.status:
        show_status(db)
        db.close()
        return

    if args.loop:
        print(f"Polling every {args.interval}s for {args.tier or 'any'} tasks...")
        try:
            while True:
                did_work = run_one(db, args.tier, args.dry_run)
                if not did_work:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        if not run_one(db, args.tier, args.dry_run):
            print("No claimable tasks.")

    db.close()


if __name__ == "__main__":
    main()
