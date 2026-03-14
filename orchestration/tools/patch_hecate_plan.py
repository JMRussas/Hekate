#!/usr/bin/env python3
"""Patch the Hecate execution plan in the orchestration database.

Fixes:
1. Reset RUNNING tasks that were paused mid-execution
2. Reassign model tiers to distribute across claude/gemini/codex/ollama
3. Add review gate tasks at wave boundaries

Usage:
    python orchestration/tools/patch_hecate_plan.py          # Apply patches
    python orchestration/tools/patch_hecate_plan.py --dry-run # Preview only
"""

import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "orchestration.db"

# --- Tier reassignments ---
# Map task_id -> new model_tier based on language domain and task type
TIER_REASSIGNMENTS = {
    # C# / Roslyn work -> Claude Code (strongest at C#)
    "b24044739164": "claude_code",   # Migrate decide tool to index-first reads
    "9225edc6f7ed": "claude_code",   # Migrate navigation tools to index-first reads
    "74f02a12c5e3": "claude_code",   # EphemeralWorkspaceFactory with TTL cache
    "deb45381cf6b": "claude_code",   # Remove persistent workspace from WorkspaceCache
    "69df039b21c1": "claude_code",   # Incremental indexing via git diff
    "31b83f54490b": "claude_code",   # NobodyIndexWriter: push index to graph DB
    "4937fe6ed140": "claude_code",   # Graph-backed where and find_implementations
    "dd5d56a37881": "claude_code",   # Cross-project analysis via Nobody graph
    "9b589a8c71db": "claude_code",   # Mixed-language project support
    "3d544acedf0e": "claude_code",   # Multi-language integration tests

    # Python work -> Gemini CLI (strong Python, has RAG access)
    "3142a1be1723": "gemini_cli",    # Tree-sitter Python parser integration
    "396ee91ff0e8": "gemini_cli",    # Jedi subprocess backend for Python semantics
    "00c1c6e5e33f": "gemini_cli",    # Python pattern heuristics and allocation detection
    "a3b7c1192e6d": "gemini_cli",    # Python worker integration tests

    # TypeScript / C++ / Frontend -> Codex CLI (strong TS/C++, web search)
    "2ab40902887b": "codex_cli",     # TypeScript worker with Tree-sitter + ts-morph
    "1e27c2177242": "codex_cli",     # C++ worker with Tree-sitter + clangd
    "7799f4b05820": "codex_cli",     # Dashboard integration in Nobody frontend

    # Mechanical / boilerplate -> Ollama (4090 local, fast, free)
    "15bd7f3540f7": "ollama",        # WorkerManager: health checks and lifecycle
    "fee4361aa0fe": "ollama",        # End-to-end roadmap validation
}

# --- Status fixes ---
# Tasks that were RUNNING when paused — reset to pending so they re-enter the queue
STATUS_RESETS = {
    "b24044739164": "pending",   # Migrate decide tool — was RUNNING, not finished
    "9225edc6f7ed": "pending",   # Migrate navigation tools — was RUNNING, not finished
}

# --- Review gate tasks to inject ---
# One per wave boundary: after wave N completes, review before wave N+1 starts
REVIEW_GATES = [
    {"after_wave": 1, "title": "Review gate: Wave 1 -> Wave 2",
     "description": (
         "Review all code changes from Wave 1 before proceeding to Wave 2.\n\n"
         "Steps:\n"
         "1. Run `dotnet build` — must compile with 0 errors, 0 warnings\n"
         "2. Run `dotnet test` — all tests must pass, no regressions\n"
         "3. Run Hecate `review` tool against all modified files\n"
         "4. Check for: naming consistency, operator precedence bugs, "
         "proper variable scoping, test coverage for new index-based code paths\n"
         "5. If violations found, create fix tasks and block Wave 2 until resolved\n\n"
         "This gate prevents compounding errors across waves."
     )},
    {"after_wave": 2, "title": "Review gate: Wave 2 -> Wave 3",
     "description": (
         "Review all code changes from Wave 2 before proceeding to Wave 3.\n\n"
         "Steps:\n"
         "1. Run `dotnet build` and `dotnet test` — full pass required\n"
         "2. Verify EphemeralWorkspaceFactory TTL behavior with a manual test\n"
         "3. Verify Tree-sitter parser produces correct AST for sample files\n"
         "4. Run Hecate `check_contracts` on all new types\n"
         "5. Check worker health endpoints respond correctly\n\n"
         "Wave 3 depends on correct workspace and parser infrastructure."
     )},
    {"after_wave": 4, "title": "Review gate: Wave 4 -> Wave 5",
     "description": (
         "Review all code changes from Waves 3-4 before graph integration.\n\n"
         "Steps:\n"
         "1. Full build + test pass\n"
         "2. Verify Python worker processes real Python files correctly\n"
         "3. Verify incremental indexing detects stale files via git diff\n"
         "4. Run Hecate `project_graph` and verify multi-project dependencies\n"
         "5. Integration test: index a mixed-language project\n\n"
         "Wave 5 introduces graph DB integration — must have solid foundations."
     )},
]


def main():
    parser = argparse.ArgumentParser(description="Patch Hecate execution plan")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    # Find the Hecate project (the one with the most tasks — the seeded plan)
    row = db.execute(
        "SELECT project_id, plan_id, COUNT(*) as cnt FROM tasks "
        "GROUP BY project_id ORDER BY cnt DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("ERROR: No tasks found in database")
        db.close()
        return
    project_id = row["project_id"]
    plan_id = row["plan_id"]
    print(f"Target project: {project_id} ({row['cnt']} tasks)")

    now = time.time()

    print("=" * 70)
    print("HECATE PLAN PATCH")
    print("=" * 70)

    # --- 1. Status resets ---
    print("\n--- Status Resets ---")
    for task_id, new_status in STATUS_RESETS.items():
        task = db.execute("SELECT title, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task:
            print(f"  {task['title']}: {task['status']} -> {new_status}")
            if not args.dry_run:
                db.execute(
                    "UPDATE tasks SET status = ?, started_at = NULL, claimed_by = NULL, "
                    "claimed_at = NULL, updated_at = ? WHERE id = ?",
                    (new_status, now, task_id),
                )

    # --- 2. Tier reassignments ---
    print("\n--- Tier Reassignments ---")
    changes_by_tier = {"claude_code": 0, "gemini_cli": 0, "codex_cli": 0, "ollama": 0}
    for task_id, new_tier in TIER_REASSIGNMENTS.items():
        task = db.execute("SELECT title, model_tier, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task and task["status"] not in ("completed",):
            old_tier = task["model_tier"]
            if old_tier != new_tier:
                print(f"  {task['title']}: {old_tier} -> {new_tier}")
                changes_by_tier[new_tier] += 1
                if not args.dry_run:
                    db.execute(
                        "UPDATE tasks SET model_tier = ?, updated_at = ? WHERE id = ?",
                        (new_tier, now, task_id),
                    )
        elif task and task["status"] == "completed":
            print(f"  {task['title']}: SKIP (already completed)")

    print(f"\n  Distribution: {dict(changes_by_tier)}")

    # --- 3. Review gate tasks ---
    print("\n--- Review Gates ---")
    for gate in REVIEW_GATES:
        wave = gate["after_wave"]
        title = gate["title"]

        # Check if already exists
        existing = db.execute(
            "SELECT id FROM tasks WHERE title = ? AND project_id = ?",
            (title, project_id),
        ).fetchone()
        if existing:
            print(f"  {title}: SKIP (already exists)")
            continue

        gate_id = uuid.uuid4().hex[:12]
        # Review gates go at the start of the next wave with high priority
        gate_wave = wave + 1
        print(f"  INSERT: {title} (wave {gate_wave}, priority 1)")

        if not args.dry_run:
            db.execute(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, "
                "task_type, priority, status, model_tier, wave, max_tokens, "
                "retry_count, max_retries, context_json, tools_json, system_prompt, "
                "requirement_ids_json, output_artifacts_json, prompt_tokens, "
                "completion_tokens, cost_usd, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gate_id, project_id, plan_id, title, gate["description"],
                    "integration",  # task_type
                    1,              # priority (runs first in its wave)
                    "blocked",      # status (blocked until prior wave completes)
                    "claude_code",  # model_tier (Claude Code for review — needs MCP tools)
                    gate_wave,      # wave
                    4096,           # max_tokens
                    0, 2,           # retry_count, max_retries
                    "[]", "[]", "", "[]", "[]",  # context, tools, system_prompt, req_ids, artifacts
                    0, 0, 0.0,      # tokens, cost
                    now, now,       # timestamps
                ),
            )

    # --- 4. Summary ---
    print("\n--- Final Task Distribution ---")
    rows = db.execute(
        "SELECT model_tier, status, COUNT(*) as cnt FROM tasks "
        "WHERE project_id = ? GROUP BY model_tier, status ORDER BY model_tier, status",
        (project_id,),
    ).fetchall()
    for r in rows:
        print(f"  {r['model_tier']:12} | {r['status']:12} | {r['cnt']}")

    total = db.execute(
        "SELECT COUNT(*) as total FROM tasks WHERE project_id = ?",
        (project_id,),
    ).fetchone()["total"]
    completed = db.execute(
        "SELECT COUNT(*) as total FROM tasks WHERE project_id = ? AND status = 'completed'",
        (project_id,),
    ).fetchone()["total"]
    print(f"\n  Total: {total} tasks ({completed} completed, {total - completed} remaining)")

    if args.dry_run:
        print("\n  [DRY RUN — no changes applied]")
    else:
        db.commit()
        print("\n  [APPLIED — changes committed to database]")

    db.close()


if __name__ == "__main__":
    main()
