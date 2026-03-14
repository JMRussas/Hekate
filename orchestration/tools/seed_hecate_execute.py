#  Nobody - Hecate Roadmap Executor Seeder
#
#  Seeds the full Hecate MCP roadmap into the orchestration engine (SQLite)
#  as an L3 project with phased plan, task decomposition, and wave scheduling.
#  This is the executable counterpart to seed_hecate_roadmap.py (PostgreSQL).
#
#  Usage:
#    python orchestration/tools/seed_hecate_execute.py                # seed + decompose into tasks
#    python orchestration/tools/seed_hecate_execute.py --dry-run      # print plan JSON
#    python orchestration/tools/seed_hecate_execute.py --draft        # seed plan without decomposing
#
#  Depends on: backend/db/connection.py, backend/services/decomposer.py
#  Used by:    manual invocation

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.connection import Database
from backend.services.decomposer import DecomposerService

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "orchestration.db"

# ---------------------------------------------------------------------------
# Requirements (numbered for traceability in tasks)
# ---------------------------------------------------------------------------

REQUIREMENTS = """\
Build a unified multi-language code intelligence MCP server (Hecate) that:

[R1] Index-first architecture: All read-only tools query a local index (.hecate/index/) \
instead of holding a live Roslyn workspace. Ephemeral workspaces open only for writes \
(execute tool, build_index) and dispose immediately.

[R2] Multi-language analysis: Support C# (Roslyn, done), Python (jedi), TypeScript/JS \
(tsserver), and C++ (clangd) via a two-tier architecture — Tree-sitter for universal \
structural analysis, optional per-language semantic backends as subprocess workers.

[R3] Distributed worker protocol: HTTP-based worker transport so language backends can \
run on remote machines (3090 server, cloud). Session-based lifecycle with health checks \
and graceful shutdown.

[R4] Nobody graph integration: Push index data into Nobody's context store (PostgreSQL + \
AGE + pgvector) for cross-project queries, semantic search, and multi-hop graph traversal.

[R5] Production hardening: Incremental indexing (only re-analyze changed files), mixed-language \
project support, cross-project analysis, and a dashboard for fleet monitoring.
"""

# ---------------------------------------------------------------------------
# L3 plan (phased, with risks + test strategy)
# Task indices are global across all phases (0-based), used in depends_on.
# ---------------------------------------------------------------------------

PLAN = {
    "summary": (
        "Complete the Hecate MCP roadmap: migrate to index-first reads, add Python/TS/C++ "
        "language backends via distributed workers, integrate with Nobody's graph DB, and "
        "harden for production use. 5 phases, 24 tasks, building on 17K lines, 306 tests, "
        "15 MCP tools, and the worker protocol Phase 1."
    ),
    "phases": [
        # ================================================================
        # Phase 1: Index-First Migration (tasks 0-5)
        # ================================================================
        {
            "name": "Index-First Migration",
            "description": (
                "Migrate all read-only tools from live Roslyn workspace to local index. "
                "Eliminates file locking, enables concurrent analysis, prerequisite for workers."
            ),
            "tasks": [
                {
                    # Task 0
                    "title": "IndexCache with lazy loading and staleness detection",
                    "description": (
                        "ConcurrentDictionary wrapper around ProjectIndexReader. Lazy-loads index "
                        "on first access, checks staleness on subsequent calls. Thread-safe."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "Unit tests: cache miss loads, cache hit returns same, stale detection, concurrent access safe.",
                    "affected_files": ["src/Analysis/Indexing/IndexCache.cs", "tests/HecateMcp.Tests.Unit/IndexCacheTests.cs"],
                },
                {
                    # Task 1
                    "title": "Migrate decide tool to index-first reads",
                    "description": (
                        "DecideTools.decide() reads from IProjectIndex instead of CSharpAnalyzer."
                        "AnalyzeFileAsync(). Falls back to live workspace if index missing/stale."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [0],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "decide() returns same verdict from index as from live workspace.",
                    "affected_files": ["src/Server/DecideTools.cs", "src/Server/ToolHelpers.cs"],
                },
                {
                    # Task 2
                    "title": "Migrate navigation tools to index-first reads",
                    "description": (
                        "where, find_implementations, find_usages, project_graph use IProjectIndex. "
                        "where() uses IndexSymbolEntry keywords, find_implementations uses IMPLEMENTS edges."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [0],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "All navigation tools return equivalent results from index vs live workspace.",
                    "affected_files": ["src/Server/NavigationTools.cs", "src/Server/WorkspaceCache.cs"],
                },
                {
                    # Task 3
                    "title": "Migrate verification tools to index reads",
                    "description": (
                        "check_contracts uses index for interface member lookup + DI scan. "
                        "test_impact uses index call graph edges. verify() stays on workspace."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [0],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "check_contracts and test_impact work from index. verify() falls back to workspace.",
                    "affected_files": ["src/Server/VerificationTools.cs"],
                },
                {
                    # Task 4
                    "title": "EphemeralWorkspaceFactory with TTL cache",
                    "description": (
                        "Opens MSBuildWorkspace on demand for writes. TTL cache (60s), disposes on "
                        "expiry. Thread-safe via SemaphoreSlim per project path."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [1, 2, 3],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "Factory creates workspace, TTL expiry disposes, concurrent requests share instance.",
                    "affected_files": ["src/Analysis/EphemeralWorkspaceFactory.cs", "tests/HecateMcp.Tests.Unit/EphemeralWorkspaceFactoryTests.cs"],
                },
                {
                    # Task 5
                    "title": "Remove persistent workspace from WorkspaceCache",
                    "description": (
                        "WorkspaceCache becomes routing layer: index reads via IndexCache, writes "
                        "via EphemeralWorkspaceFactory. No long-lived Roslyn workspaces."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [4],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1"],
                    "verification_criteria": "No persistent workspace after tool calls. All existing 306 tests pass.",
                    "affected_files": ["src/Server/WorkspaceCache.cs", "src/Server/DecideTools.cs", "src/Server/NavigationTools.cs"],
                },
            ],
        },
        # ================================================================
        # Phase 2: Worker HTTP Transport (tasks 6-9)
        # ================================================================
        {
            "name": "Worker HTTP Transport & Lifecycle",
            "description": (
                "HTTP transport for remote workers + lifecycle management. "
                "Phase 1 (IWorkerClient, RemoteAnalyzer, InProcessWorkerClient) already shipped."
            ),
            "tasks": [
                {
                    # Task 6
                    "title": "HttpWorkerClient implementation",
                    "description": (
                        "IWorkerClient over HTTP/JSON. POST /session to open, "
                        "POST /session/{id}/invoke for calls, DELETE /session/{id} to close. "
                        "IHttpClientFactory, timeout, retry on transient failures."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R3"],
                    "verification_criteria": "Unit tests with MockHttpHandler. Integration test against test HTTP server.",
                    "affected_files": ["src/Analysis/Workers/HttpWorkerClient.cs", "tests/HecateMcp.Tests.Unit/HttpWorkerClientTests.cs"],
                },
                {
                    # Task 7
                    "title": "Worker configuration in ServerConfig",
                    "description": (
                        "Add workers[] to ~/.hecate/config.json: {name, language, url, healthEndpoint}. "
                        "ServerConfigLoader parses into WorkerConfig[]. AnalyzerRegistry creates RemoteAnalyzer."
                    ),
                    "task_type": "code",
                    "complexity": "simple",
                    "depends_on": [6],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R3"],
                    "verification_criteria": "ServerConfigLoader parses workers. AnalyzerRegistry routes to RemoteAnalyzer.",
                    "affected_files": ["src/Core/LlmConfig.cs", "src/Core/ServerConfigLoader.cs", "src/Core/AnalyzerRegistry.cs"],
                },
                {
                    # Task 8
                    "title": "WorkerManager: health checks and lifecycle",
                    "description": (
                        "Tracks workers, periodic health checks (GET /health), marks online/offline/degraded. "
                        "Exposes status for dashboard. Does NOT dispatch tasks."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [6, 7],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R3"],
                    "verification_criteria": "Health check marks status correctly, periodic check runs, degraded on timeout.",
                    "affected_files": ["src/Analysis/Workers/WorkerManager.cs", "tests/HecateMcp.Tests.Unit/WorkerManagerTests.cs"],
                },
                {
                    # Task 9
                    "title": "Python worker scaffold (ASP.NET minimal API)",
                    "description": (
                        "Minimal worker process implementing session HTTP protocol. "
                        "Tree-sitter Tier 1 structural analysis. No jedi yet."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [6],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2", "R3"],
                    "verification_criteria": "Worker starts, /health responds, session open/invoke/close works. Tier 1 for .py.",
                    "affected_files": ["workers/python/Program.cs", "workers/python/PythonWorker.csproj"],
                },
            ],
        },
        # ================================================================
        # Phase 3: Python Language Backend (tasks 10-13)
        # ================================================================
        {
            "name": "Python Language Backend",
            "description": "Full Python analysis — Tree-sitter Tier 1 + jedi Tier 2. Runs as HTTP worker.",
            "tasks": [
                {
                    # Task 10
                    "title": "Tree-sitter Python parser integration",
                    "description": (
                        "Structural analysis: classes, functions, imports, decorators, type hints. "
                        "Pattern heuristics: dataclass, ABC, singleton, context-manager, decorator-chain."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [9],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2"],
                    "verification_criteria": "Parses real .py files, extracts types/methods, detects Python patterns.",
                    "affected_files": ["workers/python/TreeSitterPython.cs", "tests/HecateMcp.Tests.Unit/PythonAnalyzerTests.cs"],
                },
                {
                    # Task 11
                    "title": "Jedi subprocess backend for Python semantics",
                    "description": (
                        "Spawn jedi as Python subprocess (JSON over stdin/stdout). Tier 2: "
                        "find_implementations, find_usages, go-to-definition, type inference."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [10],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2"],
                    "verification_criteria": "Jedi resolves references. find_implementations finds subclasses.",
                    "affected_files": ["workers/python/JediBackend.cs", "workers/python/jedi_bridge.py"],
                },
                {
                    # Task 12
                    "title": "Python pattern heuristics and allocation detection",
                    "description": (
                        "Global mutable state, async generators, heavy decorators. "
                        "Allocation-equivalent: list comprehensions in loops, string concat in loops."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [10],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2"],
                    "verification_criteria": "Detects Python patterns. No false positives on common idioms.",
                    "affected_files": ["workers/python/PythonPatterns.cs"],
                },
                {
                    # Task 13
                    "title": "Python worker integration tests",
                    "description": (
                        "E2E: start Python worker, connect via HttpWorkerClient, run all 12 "
                        "ILanguageAnalyzer methods through full stack."
                    ),
                    "task_type": "integration",
                    "complexity": "medium",
                    "depends_on": [10, 11, 12],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2", "R3"],
                    "verification_criteria": "All ILanguageAnalyzer methods return valid results through HTTP.",
                    "affected_files": ["tests/HecateMcp.Tests.Integration/PythonWorkerTests.cs"],
                },
            ],
        },
        # ================================================================
        # Phase 4: TypeScript/JS & C++ (tasks 14-16)
        # ================================================================
        {
            "name": "TypeScript/JS & C++ Backends",
            "description": "TS/JS (tsserver) and C++ (clangd) workers. Same architecture as Python. Can parallelize.",
            "tasks": [
                {
                    # Task 14
                    "title": "TypeScript worker with Tree-sitter + ts-morph",
                    "description": (
                        "TS/JS/TSX/JSX. Tree-sitter for structure, ts-morph for semantics. "
                        "Patterns: react-component, express-middleware, event-emitter, barrel-export."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [6, 9],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2"],
                    "verification_criteria": "Analyzes sample TS project. Detects React components. find_implementations for TS interfaces.",
                    "affected_files": ["workers/typescript/Program.cs", "workers/typescript/TsMorphBackend.cs"],
                },
                {
                    # Task 15
                    "title": "C++ worker with Tree-sitter + clangd",
                    "description": (
                        "Tree-sitter for structure, clangd LSP for semantics. Patterns: RAII, PIMPL, "
                        "ISR handlers. Constraints: no-heap, no-exceptions. compile_commands.json."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [6, 9],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2"],
                    "verification_criteria": "Analyzes sample C++ project. Detects RAII/PIMPL. Enforces constraints.",
                    "affected_files": ["workers/cpp/Program.cs", "workers/cpp/ClangdBackend.cs"],
                },
                {
                    # Task 16
                    "title": "Multi-language integration tests",
                    "description": (
                        "All 4 workers running. Mixed-language project (C# + Python + TS). "
                        "AnalyzerRegistry routes correctly. Cross-language find_usages."
                    ),
                    "task_type": "integration",
                    "complexity": "complex",
                    "depends_on": [13, 14, 15],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2", "R3"],
                    "verification_criteria": "All 4 backends respond through HTTP. Mixed-project analysis works.",
                    "affected_files": ["tests/HecateMcp.Tests.Integration/MultiLanguageTests.cs"],
                },
            ],
        },
        # ================================================================
        # Phase 5: Nobody Integration & Hardening (tasks 17-23)
        # ================================================================
        {
            "name": "Nobody Integration & Production Hardening",
            "description": "Graph DB integration, cross-project queries, incremental indexing, dashboard.",
            "tasks": [
                {
                    # Task 17
                    "title": "NobodyIndexWriter: push index to graph DB",
                    "description": (
                        "IndexData → Nobody graph nodes/edges via REST. Idempotent. "
                        "CompositeIndexWriter writes local + Nobody simultaneously."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [5],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R4"],
                    "verification_criteria": "Index data in Nobody graph. Idempotent re-index works.",
                    "affected_files": ["src/Analysis/Indexing/NobodyIndexWriter.cs", "src/Analysis/Indexing/CompositeIndexWriter.cs"],
                },
                {
                    # Task 18
                    "title": "Graph-backed where and find_implementations",
                    "description": (
                        "where() uses pgvector fuzzy search + AGE multi-hop traversal. "
                        "find_implementations uses IMPLEMENTS edges. Falls back to local index."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [17],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R4"],
                    "verification_criteria": "Navigation tools return results from graph. Fallback to local index works.",
                    "affected_files": ["src/Server/NavigationTools.cs", "src/Analysis/NobodyGraphStore.cs"],
                },
                {
                    # Task 19
                    "title": "Incremental indexing via git diff",
                    "description": (
                        "build_index --incremental. Git diff or mtime to find changed files. "
                        "Merge into existing index. Handle deleted files."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [5],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R5"],
                    "verification_criteria": "Only processes changed files. Result matches full reindex.",
                    "affected_files": ["src/Analysis/Indexing/IncrementalIndexer.cs", "src/Analysis/GraphIndexer.cs"],
                },
                {
                    # Task 20
                    "title": "Mixed-language project support",
                    "description": (
                        "Per-pathRule language override in .hecate.json schema v2. "
                        "build_index iterates all configured analyzers. Blazor (C# + TS)."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [16],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R2", "R5"],
                    "verification_criteria": "Mixed C#+TS project indexes both. Path rules override detection.",
                    "affected_files": ["src/Core/Types.cs", "src/Core/AnalyzerRegistry.cs"],
                },
                {
                    # Task 21
                    "title": "Cross-project analysis via Nobody graph",
                    "description": (
                        "Query across all indexed projects. Cross-project find_usages, "
                        "dependency graph, shared pattern detection."
                    ),
                    "task_type": "code",
                    "complexity": "complex",
                    "depends_on": [18],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R4", "R5"],
                    "verification_criteria": "find_usages returns from multiple projects. Cross-project edges in graph.",
                    "affected_files": ["src/Server/NavigationTools.cs", "src/Analysis/NobodyGraphStore.cs"],
                },
                {
                    # Task 22
                    "title": "Dashboard integration in Nobody frontend",
                    "description": (
                        "Fleet status cards for workers. Architecture view: project → worker → language. "
                        "Index status per project."
                    ),
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [8],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R5"],
                    "verification_criteria": "Dashboard shows worker fleet, architecture view, index status.",
                    "affected_files": ["context-store/ui/src/components/HecateDashboard.tsx"],
                },
                {
                    # Task 23
                    "title": "End-to-end roadmap validation",
                    "description": (
                        "Full E2E: multi-language index, push to Nobody graph, query all 15 MCP tools, "
                        "incremental reindex, cross-project query."
                    ),
                    "task_type": "integration",
                    "complexity": "complex",
                    "depends_on": [17, 18, 19, 20, 21],
                    "tools_needed": ["read_file", "write_file"],
                    "requirement_ids": ["R1", "R2", "R3", "R4", "R5"],
                    "verification_criteria": "All 15 MCP tools work with index-first, distributed workers, and graph.",
                    "affected_files": ["tests/HecateMcp.Tests.Integration/EndToEndTests.cs"],
                },
            ],
        },
    ],
    "open_questions": [
        {
            "question": "Should Python/TS/C++ workers be C# processes or native-language processes?",
            "proposed_answer": "C# with Tree-sitter .NET for Tier 1, subprocess IPC to native backends for Tier 2.",
            "impact": "Native workers need separate protocol implementations per language.",
        },
        {
            "question": "Should incremental indexing use git diff or filesystem mtime?",
            "proposed_answer": "Both — git diff in repos, mtime fallback. StalenessChecker has mtime already.",
            "impact": "Git-only means non-git projects can't use incremental indexing.",
        },
        {
            "question": "Should cross-project queries require same Nobody instance?",
            "proposed_answer": "Yes for now. Federation is a future concern.",
            "impact": "Multiple Nobody instances can't query across each other.",
        },
    ],
    "risk_assessment": [
        {
            "risk": "Tree-sitter .NET bindings may be immature on Windows",
            "likelihood": "medium",
            "impact": "high",
            "mitigation": "Evaluate early in Phase 3. Fallback: tree-sitter CLI subprocess.",
        },
        {
            "risk": "Subprocess IPC (jedi/tsserver/clangd) adds latency and failure modes",
            "likelihood": "medium",
            "impact": "medium",
            "mitigation": "Session protocol handles reconnection. Health checks. Fallback to Tier 1.",
        },
        {
            "risk": "Index-first migration may diverge from live workspace on edge cases",
            "likelihood": "high",
            "impact": "medium",
            "mitigation": "Run both paths in parallel, compare. Document divergences.",
        },
        {
            "risk": "Nobody graph may not handle 100K+ nodes",
            "likelihood": "low",
            "impact": "high",
            "mitigation": "pgvector + AGE proven at scale. Add limits and pagination.",
        },
    ],
    "test_strategy": {
        "approach": "Each phase has integration tests. Unit tests for all new components. Existing 306 tests must pass throughout.",
        "test_tasks": [
            "Python worker integration tests",
            "Multi-language integration tests",
            "End-to-end roadmap validation",
        ],
        "coverage_notes": "Focus on cross-boundary: index↔workspace parity, HTTP↔in-process parity, local↔graph parity.",
    },
}


async def main():
    parser = argparse.ArgumentParser(description="Seed Hecate roadmap into orchestration engine (SQLite)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan JSON without inserting")
    parser.add_argument("--draft", action="store_true", help="Insert plan as draft (don't decompose into tasks)")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(PLAN, indent=2))
        total = sum(len(p["tasks"]) for p in PLAN["phases"])
        waves_est = max(
            max(t.get("depends_on", [-1]) or [-1]) for p in PLAN["phases"] for t in p["tasks"]
        ) + 2  # rough estimate
        print(f"\n--- {total} tasks across {len(PLAN['phases'])} phases, ~{waves_est} waves ---")
        return

    db = Database()
    await db.init(str(DB_PATH), run_migrations=False)

    try:
        now = time.time()
        project_id = uuid.uuid4().hex[:12]
        plan_id = uuid.uuid4().hex[:12]

        # Insert project
        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, "
            "repo_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                "Hecate MCP — Full Roadmap",
                REQUIREMENTS,
                "draft",
                json.dumps({"planning_rigor": "L3", "execution_mode": "auto"}),
                "C:\\Users\\kmgsp\\Documents\\git\\hecate-mcp",
                now, now,
            ),
        )
        print(f"Project: {project_id}")

        # Insert plan
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan_id, project_id, 1, "claude-opus-4-6/manual", json.dumps(PLAN), "draft", now),
        )

        total_tasks = sum(len(p["tasks"]) for p in PLAN["phases"])
        print(f"Plan:    {plan_id} ({total_tasks} tasks across {len(PLAN['phases'])} phases)")

        if args.draft:
            print(f"\nPlan is DRAFT. Approve via:")
            print(f"  POST /projects/{project_id}/plans/{plan_id}/approve")
            return

        # Decompose into executable tasks with waves
        decomposer = DecomposerService(db=db)
        result = await decomposer.decompose(project_id, plan_id)
        print(f"Tasks:   {result['tasks_created']} created, {result['total_waves']} waves, est ${result['estimated_cost_usd']:.2f}")

        # Show wave breakdown
        rows = await db.fetchall(
            "SELECT wave, count(*), group_concat(title, ' | ') FROM tasks "
            "WHERE project_id = ? GROUP BY wave ORDER BY wave",
            (project_id,),
        )
        print(f"\nWave breakdown:")
        for row in rows:
            print(f"  Wave {row[0]}: {row[1]} tasks — {row[2]}")

        print(f"\nReady for execution. Start via:")
        print(f"  POST /projects/{project_id}/execute")

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
