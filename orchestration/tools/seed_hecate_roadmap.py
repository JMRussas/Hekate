#  Nobody - Hecate Roadmap Seeder
#
#  Seeds the full Hecate MCP roadmap into the Nobody context store (PostgreSQL)
#  as a plan node tree: plan → plan_phase → plan_step → task, with risks,
#  questions, test_specs, and a retrospective placeholder.
#
#  Matches the C# seeder pattern (PlanSeeder.cs, Plan005Seeder.cs).
#
#  Usage:
#    python orchestration/tools/seed_hecate_roadmap.py              # seed into context store
#    python orchestration/tools/seed_hecate_roadmap.py --dry-run    # print node tree without inserting
#    python orchestration/tools/seed_hecate_roadmap.py --base-url http://192.168.1.164:5102
#
#  Depends on: Context store REST API (POST /api/projects, /api/project/{id}/nodes, /api/node/{id}/children)
#  Used by:    manual invocation

import argparse
import json
import sys
import requests

DEFAULT_BASE_URL = "http://localhost:5102"


# ---------------------------------------------------------------------------
# REST API helpers — match context store endpoints
# ---------------------------------------------------------------------------

class ContextStoreClient:
    """Thin wrapper around the Nobody context store REST API."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.nodes_created = 0

    def create_project(self, name: str, root_path: str | None = None) -> str:
        """POST /api/projects — idempotent, returns project ID."""
        r = self.session.post(f"{self.base}/api/projects", json={
            "name": name,
            "rootPath": root_path,
        })
        r.raise_for_status()
        data = r.json()
        return data["id"]

    def create_root_node(self, project_id: str, node_type: str, name: str,
                         value: str | None = None,
                         attrs: dict[str, str] | None = None) -> str:
        """POST /api/project/{projectId}/nodes — create root-level node."""
        r = self.session.post(f"{self.base}/api/project/{project_id}/nodes", json={
            "nodeType": node_type,
            "name": name,
            "value": value,
            "attributes": attrs,
        })
        r.raise_for_status()
        self.nodes_created += 1
        return r.json()["id"]

    def create_child(self, parent_id: str, node_type: str, name: str,
                     value: str | None = None,
                     attrs: dict[str, str] | None = None) -> str:
        """POST /api/node/{parentId}/children — create child node."""
        r = self.session.post(f"{self.base}/api/node/{parent_id}/children", json={
            "nodeType": node_type,
            "name": name,
            "value": value,
            "attributes": attrs,
        })
        r.raise_for_status()
        self.nodes_created += 1
        return r.json()["node"]["id"]


# ---------------------------------------------------------------------------
# Roadmap data
# ---------------------------------------------------------------------------

PLAN_SUMMARY = (
    "Complete the Hecate MCP roadmap: migrate to index-first reads, add Python/TS/C++ "
    "language backends via distributed workers, integrate with Nobody's graph DB, and "
    "harden for production use. 5 phases, 24 tasks, building on the existing foundation "
    "of 17K lines, 306 tests, 15 MCP tools, and the worker protocol Phase 1."
)

PHASES = [
    {
        "name": "Index-First Migration",
        "description": (
            "Migrate all read-only tools from live Roslyn workspace to the local index. "
            "Eliminates file locking, enables concurrent analysis, prerequisite for distributed workers."
        ),
        "status": "pending",
        "steps": [
            {
                "name": "IndexCache with lazy loading",
                "description": "ConcurrentDictionary wrapper around ProjectIndexReader. Lazy-loads on first access, checks staleness on subsequent calls. Thread-safe, non-blocking.",
                "status": "pending",
                "tasks": [
                    ("Implement IndexCache.cs", "Wrap ProjectIndexReader with ConcurrentDictionary keyed by normalized project path"),
                    ("Unit tests for IndexCache", "Cache miss loads, cache hit returns same, stale detection, concurrent access"),
                ],
                "affected_files": "src/Analysis/Indexing/IndexCache.cs",
            },
            {
                "name": "Migrate decide tool to index reads",
                "description": "DecideTools.decide() reads from IProjectIndex instead of CSharpAnalyzer.AnalyzeFileAsync(). Falls back to live workspace if index missing/stale.",
                "status": "pending",
                "tasks": [
                    ("Refactor decide() to use IProjectIndex", "File analysis, patterns, allocations, hot-path data all from index"),
                    ("Integration test: index vs workspace parity", "decide() returns same verdict from index as from live workspace"),
                ],
                "affected_files": "src/Server/DecideTools.cs, src/Server/ToolHelpers.cs",
            },
            {
                "name": "Migrate navigation tools to index reads",
                "description": "where, find_implementations, find_usages, project_graph use IProjectIndex symbol lookups and edge queries.",
                "status": "pending",
                "tasks": [
                    ("Refactor navigation tools", "where() uses IndexSymbolEntry keywords, find_implementations uses IMPLEMENTS edges, project_graph uses index deps"),
                    ("Integration tests for navigation parity", "All navigation tools return equivalent results from index vs live workspace"),
                ],
                "affected_files": "src/Server/NavigationTools.cs, src/Server/WorkspaceCache.cs",
            },
            {
                "name": "Migrate verification tools",
                "description": "check_contracts uses index for interface member lookup. test_impact uses index call graph edges. verify() stays on workspace.",
                "status": "pending",
                "tasks": [
                    ("Refactor check_contracts and test_impact", "Prefer index, fall back to workspace"),
                ],
                "affected_files": "src/Server/VerificationTools.cs",
            },
            {
                "name": "EphemeralWorkspaceFactory",
                "description": "Opens MSBuildWorkspace on demand for writes (execute, verify, build_index). TTL cache (60s), disposes on expiry, thread-safe via SemaphoreSlim.",
                "status": "pending",
                "tasks": [
                    ("Implement EphemeralWorkspaceFactory", "TTL-cached workspace creation with SemaphoreSlim per project path"),
                    ("Unit + integration tests", "Factory creates workspace, TTL expiry disposes, concurrent requests share instance"),
                ],
                "affected_files": "src/Analysis/EphemeralWorkspaceFactory.cs",
            },
            {
                "name": "Remove persistent workspace",
                "description": "WorkspaceCache becomes a routing layer: index reads via IndexCache, writes via EphemeralWorkspaceFactory. No long-lived Roslyn workspaces.",
                "status": "pending",
                "tasks": [
                    ("Refactor WorkspaceCache", "Route reads to IndexCache, writes to EphemeralWorkspaceFactory"),
                    ("Verify all existing tests still pass", "No persistent workspace in memory after tool calls complete"),
                ],
                "affected_files": "src/Server/WorkspaceCache.cs",
            },
        ],
    },
    {
        "name": "Worker HTTP Transport & Lifecycle",
        "description": (
            "HTTP transport for remote workers and lifecycle management. "
            "Phase 1 (IWorkerClient, RemoteAnalyzer, InProcessWorkerClient) already shipped."
        ),
        "status": "pending",
        "steps": [
            {
                "name": "HttpWorkerClient implementation",
                "description": "IWorkerClient over HTTP/JSON. POST /session to open, POST /session/{id}/invoke for calls, DELETE /session/{id} to close. IHttpClientFactory.",
                "status": "pending",
                "tasks": [
                    ("Implement HttpWorkerClient.cs", "HttpClient with timeout, retry on transient failures, connection pooling"),
                    ("Unit tests with MockHttpHandler", "Session open/invoke/close, timeout handling, retry logic"),
                ],
                "affected_files": "src/Analysis/Workers/HttpWorkerClient.cs",
            },
            {
                "name": "Worker configuration in ServerConfig",
                "description": "Add workers section to ~/.hecate/config.json: array of {name, language, url, healthEndpoint}. AnalyzerRegistry creates RemoteAnalyzer from config.",
                "status": "pending",
                "tasks": [
                    ("Extend ServerConfigLoader + LlmConfig.cs", "Parse workers array into WorkerConfig[]"),
                    ("Wire AnalyzerRegistry to create RemoteAnalyzers", "Route configured languages to remote workers"),
                ],
                "affected_files": "src/Core/LlmConfig.cs, src/Core/ServerConfigLoader.cs, src/Core/AnalyzerRegistry.cs",
            },
            {
                "name": "WorkerManager: health checks and lifecycle",
                "description": "Tracks workers, runs periodic health checks (GET /health), marks online/offline/degraded. Exposes status for dashboard.",
                "status": "pending",
                "tasks": [
                    ("Implement WorkerManager.cs", "Periodic health checks, status tracking, degraded state on timeout"),
                    ("Unit tests for health check lifecycle", "Online/offline/degraded transitions, periodic check runs"),
                ],
                "affected_files": "src/Analysis/Workers/WorkerManager.cs",
            },
            {
                "name": "Python worker scaffold",
                "description": "Minimal ASP.NET worker process implementing session HTTP protocol. Tree-sitter Tier 1 structural analysis. No jedi yet.",
                "status": "pending",
                "tasks": [
                    ("Create Python worker project", "ASP.NET minimal API with session endpoints and TreeSitterBase logic"),
                    ("Integration test: HTTP session lifecycle", "Worker starts, responds to /health, accepts session open/invoke/close"),
                ],
                "affected_files": "workers/python/Program.cs, workers/python/PythonWorker.csproj",
            },
        ],
    },
    {
        "name": "Python Language Backend",
        "description": "Full Python analysis — Tree-sitter Tier 1 plus jedi Tier 2 semantic backend. Runs as worker process, reachable via HTTP transport.",
        "status": "pending",
        "steps": [
            {
                "name": "Tree-sitter Python parser",
                "description": "Structural analysis: classes, functions, imports, decorators, type hints. Pattern heuristics: dataclass, ABC, singleton, context-manager, decorator-chain.",
                "status": "pending",
                "tasks": [
                    ("Integrate tree-sitter-python", "Extract types, methods, detect Python-specific patterns"),
                    ("Unit tests with sample Python code", "Parse real .py files, verify pattern detection"),
                ],
                "affected_files": "workers/python/TreeSitterPython.cs",
            },
            {
                "name": "Jedi subprocess backend",
                "description": "Spawn jedi as Python subprocess (JSON over stdin/stdout). Tier 2: find_implementations, find_usages, go-to-definition, type inference.",
                "status": "pending",
                "tasks": [
                    ("Implement JediBackend.cs + jedi_bridge.py", "IPC protocol matching WorkerProtocol schema"),
                    ("Integration tests with sample Python project", "Jedi resolves references, finds subclasses"),
                ],
                "affected_files": "workers/python/JediBackend.cs, workers/python/jedi_bridge.py",
            },
            {
                "name": "Python pattern heuristics",
                "description": "Global mutable state, async generators, heavy decorators. Allocation-equivalent: list comprehensions in loops, string concat in loops.",
                "status": "pending",
                "tasks": [
                    ("Implement PythonPatterns.cs", "Detection heuristics for Python-specific patterns and allocation equivalents"),
                ],
                "affected_files": "workers/python/PythonPatterns.cs",
            },
            {
                "name": "Python worker integration tests",
                "description": "End-to-end: start worker, connect via HttpWorkerClient, run all 12 ILanguageAnalyzer methods through full stack.",
                "status": "pending",
                "tasks": [
                    ("E2E test suite for Python worker", "All ILanguageAnalyzer methods return valid results through HTTP transport"),
                ],
                "affected_files": "tests/HecateMcp.Tests.Integration/PythonWorkerTests.cs",
            },
        ],
    },
    {
        "name": "TypeScript/JS & C++ Backends",
        "description": "Add TypeScript/JS (tsserver) and C++ (clangd) language backends. Same worker architecture as Python. Can be parallelized.",
        "status": "pending",
        "steps": [
            {
                "name": "TypeScript worker",
                "description": "TS/JS/TSX/JSX. Tree-sitter for structure, ts-morph for semantics. Patterns: react-component, express-middleware, event-emitter, barrel-export.",
                "status": "pending",
                "tasks": [
                    ("Implement TypeScript worker", "Tree-sitter + ts-morph subprocess, HTTP session protocol"),
                    ("Unit + integration tests", "Analyze sample TS project, detect React components"),
                ],
                "affected_files": "workers/typescript/Program.cs, workers/typescript/TsMorphBackend.cs",
            },
            {
                "name": "C++ worker",
                "description": "Tree-sitter for structure, clangd LSP for semantics. Patterns: RAII, PIMPL, ISR handlers, ring buffers. Constraints: no-heap, no-exceptions.",
                "status": "pending",
                "tasks": [
                    ("Implement C++ worker", "Tree-sitter + clangd LSP client, compile_commands.json support"),
                    ("Unit + integration tests", "Analyze sample C++ project, detect RAII/PIMPL, enforce constraints"),
                ],
                "affected_files": "workers/cpp/Program.cs, workers/cpp/ClangdBackend.cs",
            },
            {
                "name": "Multi-language integration tests",
                "description": "All 4 workers running. Mixed-language project (C# + Python + TS). AnalyzerRegistry routes correctly.",
                "status": "pending",
                "tasks": [
                    ("E2E multi-language test suite", "All 4 backends respond through HTTP, mixed-project analysis works"),
                ],
                "affected_files": "tests/HecateMcp.Tests.Integration/MultiLanguageTests.cs",
            },
        ],
    },
    {
        "name": "Nobody Integration & Production Hardening",
        "description": "Push index data to Nobody graph DB, cross-project queries, incremental indexing, mixed-language support, dashboard.",
        "status": "pending",
        "steps": [
            {
                "name": "NobodyIndexWriter",
                "description": "Adapter: IndexData → Nobody graph nodes/edges via REST. Idempotent. CompositeIndexWriter writes local + Nobody simultaneously.",
                "status": "pending",
                "tasks": [
                    ("Implement NobodyIndexWriter + CompositeIndexWriter", "Convert IndexData to graph nodes/edges, idempotent re-index"),
                ],
                "affected_files": "src/Analysis/Indexing/NobodyIndexWriter.cs, src/Analysis/Indexing/CompositeIndexWriter.cs",
            },
            {
                "name": "Graph-backed navigation",
                "description": "where() uses pgvector fuzzy search + AGE multi-hop traversal. find_implementations uses IMPLEMENTS edges. Falls back to local index.",
                "status": "pending",
                "tasks": [
                    ("Refactor where + find_implementations for graph", "pgvector for concept matching, AGE for ownership traversal"),
                    ("Fallback tests", "Graph unavailable → local index fallback works"),
                ],
                "affected_files": "src/Server/NavigationTools.cs, src/Analysis/NobodyGraphStore.cs",
            },
            {
                "name": "Incremental indexing",
                "description": "build_index --incremental. git diff or mtime to find changed files. Merge into existing index. Handle deleted files.",
                "status": "pending",
                "tasks": [
                    ("Implement IncrementalIndexer", "Git diff + mtime comparison, merge into existing index, delete stale entries"),
                ],
                "affected_files": "src/Analysis/Indexing/IncrementalIndexer.cs, src/Analysis/GraphIndexer.cs",
            },
            {
                "name": "Mixed-language project support",
                "description": "Per-pathRule language override in .hecate.json schema v2. build_index iterates all configured analyzers. Blazor (C# + TS).",
                "status": "pending",
                "tasks": [
                    ("Schema v2 + AnalyzerRegistry multi-language routing", "Per-path language hints, multi-analyzer indexing"),
                ],
                "affected_files": "src/Core/Types.cs, src/Core/AnalyzerRegistry.cs",
            },
            {
                "name": "Cross-project analysis",
                "description": "Query across all indexed projects in Nobody graph. Cross-project find_usages, dependency graph, shared pattern detection.",
                "status": "pending",
                "tasks": [
                    ("Cross-project graph queries", "find_usages returns results from multiple projects, cross-project edges in project_graph"),
                ],
                "affected_files": "src/Server/NavigationTools.cs, src/Analysis/NobodyGraphStore.cs",
            },
            {
                "name": "Dashboard integration",
                "description": "Fleet status cards for workers. Architecture view: project → worker → language routing. Index status per project.",
                "status": "pending",
                "tasks": [
                    ("Build HecateDashboard.tsx", "Worker fleet status, architecture view, index status display"),
                ],
                "affected_files": "context-store/ui/src/components/HecateDashboard.tsx",
            },
            {
                "name": "End-to-end roadmap validation",
                "description": "Full E2E: multi-language index, push to Nobody graph, query via all 15 MCP tools, incremental reindex, cross-project query.",
                "status": "pending",
                "tasks": [
                    ("Comprehensive E2E test suite", "All 15 MCP tools work with index-first reads, distributed workers, and graph integration"),
                ],
                "affected_files": "tests/HecateMcp.Tests.Integration/EndToEndTests.cs",
            },
        ],
    },
]

RISKS = [
    {
        "description": "Tree-sitter .NET bindings may be immature or have platform issues on Windows",
        "severity": "high",
        "mitigation": "Evaluate bindings early in Phase 3. Fallback: use tree-sitter CLI subprocess.",
    },
    {
        "description": "jedi/tsserver/clangd subprocess IPC adds latency and failure modes",
        "severity": "medium",
        "mitigation": "Session-based protocol handles reconnection. Health checks detect failures. Timeout + fallback to Tier 1.",
    },
    {
        "description": "Index-first migration may produce different results than live workspace for edge cases",
        "severity": "high",
        "mitigation": "Run both paths in parallel during migration, compare results. Document known divergences.",
    },
    {
        "description": "Nobody graph DB may not handle large codebases (100K+ nodes)",
        "severity": "low",
        "mitigation": "pgvector + AGE are proven at scale. Add index limits and pagination in queries.",
    },
]

QUESTIONS = [
    {
        "text": "Should Python/TS/C++ workers be C# processes or native-language processes?",
        "proposed_answer": "C# processes with Tree-sitter .NET bindings for Tier 1, subprocess IPC to native backends for Tier 2. Keeps worker protocol uniform.",
    },
    {
        "text": "Should incremental indexing use git diff or filesystem mtime?",
        "proposed_answer": "Both — git diff in git repos (more accurate), mtime fallback otherwise. StalenessChecker already has mtime comparison.",
    },
    {
        "text": "Should cross-project queries require all projects in the same Nobody instance?",
        "proposed_answer": "Yes for now. Federation across Nobody instances is a future concern.",
    },
]

TEST_SPECS = [
    ("Index vs workspace parity: all tools return equivalent results", "integration"),
    ("HTTP transport vs in-process parity: RemoteAnalyzer matches InProcess", "integration"),
    ("Multi-language E2E: 4 workers, mixed project, all tools", "integration"),
    ("Local index vs Nobody graph parity: navigation tools return same results", "integration"),
    ("Incremental index correctness: matches full reindex after changes", "unit"),
]


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def seed(client: ContextStoreClient, dry_run: bool = False):
    """Seed the full Hecate roadmap into the context store."""

    if dry_run:
        _print_tree()
        return

    # Create or get project
    project_id = client.create_project("HecateMCP", "C:\\Users\\kmgsp\\Documents\\git\\hecate-mcp")
    print(f"Project: {project_id}")

    # Root plan node
    plan_id = client.create_root_node(project_id, "plan", "Hecate MCP — Full Roadmap", PLAN_SUMMARY, {
        "level": "L3",
        "status": "plan",
        "plan_type": "feature",
        "priority": "p1",
        "target_date": "2026-06-30",
        "created_date": "2026-03-14",
        "origin": "AI-generated roadmap from codebase analysis — Claude Opus 4.6",
        "modified_by": "claude-opus-4-6",
    })
    print(f"Plan:    {plan_id}")

    # Milestones
    client.create_child(plan_id, "milestone", "C# analysis fully operational",
                        "Core MCP server with 15 tools, 306 tests, role system, design intelligence, consensus engine",
                        {"status": "completed"})
    client.create_child(plan_id, "milestone", "Worker protocol Phase 1 shipped",
                        "IWorkerClient, RemoteAnalyzer, InProcessWorkerClient — session-based RPC",
                        {"status": "completed"})
    client.create_child(plan_id, "milestone", "Index-first reads complete",
                        "All read-only tools use local index, no persistent Roslyn workspace",
                        {"status": "pending"})
    client.create_child(plan_id, "milestone", "Multi-language analysis live",
                        "Python, TypeScript, C++ workers responding via HTTP transport",
                        {"status": "pending"})

    # Phases
    # GATHER + PLAN + APPROVE are already done (this plan IS the output)
    client.create_child(plan_id, "plan_phase", "GATHER", "Analyzed codebase, git history, existing plans, CLAUDE.md",
                        {"status": "completed"})
    client.create_child(plan_id, "plan_phase", "PLAN", "Designed 5-phase roadmap with risks and test strategy",
                        {"status": "completed"})
    approve_id = client.create_child(plan_id, "plan_phase", "APPROVE", "Awaiting user review and approval",
                                     {"status": "pending"})
    client.create_child(approve_id, "decision", "Plan seeded into context store",
                        "Roadmap stored as node tree in PostgreSQL for orchestration and tracking",
                        {"status": "committed"})

    # EXECUTE phase — contains all the real work
    execute_id = client.create_child(plan_id, "plan_phase", "EXECUTE",
                                     "Implementation: index migration, workers, languages, integration",
                                     {"status": "pending"})

    for phase_data in PHASES:
        # Each roadmap phase becomes a plan_step under EXECUTE
        step_id = client.create_child(execute_id, "plan_step", phase_data["name"], phase_data["description"],
                                      {"status": phase_data["status"]})

        for sub_step in phase_data["steps"]:
            sub_id = client.create_child(step_id, "plan_step", sub_step["name"], sub_step["description"],
                                         {"status": sub_step["status"]})

            for task_name, task_desc in sub_step["tasks"]:
                client.create_child(sub_id, "task", task_name, task_desc, {"status": "pending"})

            # Affected files as a note
            if sub_step.get("affected_files"):
                client.create_child(sub_id, "decision", "Affected files",
                                    sub_step["affected_files"], {"status": "committed"})

    # CLOSE_OUT phase
    close_out_id = client.create_child(plan_id, "plan_phase", "CLOSE_OUT",
                                       "Run full test suite, update docs, write retrospective",
                                       {"status": "pending"})

    for spec_desc, spec_type in TEST_SPECS:
        client.create_child(close_out_id, "test_spec", None, spec_desc,
                            {"test_type": spec_type, "status": "pending"})

    client.create_child(close_out_id, "retrospective", "Hecate Roadmap Retrospective", None,
                        {"status": "pending", "template": "status|test_results|deviations|learnings|docs_updated"})

    # Plan-level risks
    for risk in RISKS:
        client.create_child(plan_id, "risk", None, risk["description"],
                            {"severity": risk["severity"], "mitigation": risk["mitigation"]})

    # Plan-level questions
    for q in QUESTIONS:
        client.create_child(plan_id, "question", None, q["text"],
                            {"status": "proposed", "proposed_answer": q["proposed_answer"]})

    print(f"\nSeeded {client.nodes_created} nodes into context store")


def _print_tree():
    """Print the plan structure without inserting."""
    print("plan: Hecate MCP — Full Roadmap (L3)")
    print("├── milestone: C# analysis fully operational [completed]")
    print("├── milestone: Worker protocol Phase 1 shipped [completed]")
    print("├── milestone: Index-first reads complete [pending]")
    print("├── milestone: Multi-language analysis live [pending]")
    print("├── plan_phase: GATHER [completed]")
    print("├── plan_phase: PLAN [completed]")
    print("├── plan_phase: APPROVE [pending]")
    print("├── plan_phase: EXECUTE [pending]")

    total_tasks = 0
    for i, phase in enumerate(PHASES):
        prefix = "│   ├──" if i < len(PHASES) - 1 else "│   └──"
        print(f"{prefix} plan_step: {phase['name']}")
        for j, step in enumerate(phase["steps"]):
            sub_prefix = "│   │   ├──" if j < len(step.get("tasks", [])) else "│   │   └──"
            print(f"{sub_prefix} plan_step: {step['name']}")
            for task_name, _ in step["tasks"]:
                print(f"│   │   │   └── task: {task_name}")
                total_tasks += 1

    print("├── plan_phase: CLOSE_OUT [pending]")
    for desc, ttype in TEST_SPECS:
        print(f"│   ├── test_spec: {desc} [{ttype}]")
    print("│   └── retrospective: Hecate Roadmap Retrospective")

    print(f"├── {len(RISKS)} risks")
    print(f"└── {len(QUESTIONS)} questions")
    print(f"\n--- {total_tasks} tasks across {len(PHASES)} phases, {sum(len(p['steps']) for p in PHASES)} steps ---")


def main():
    parser = argparse.ArgumentParser(description="Seed Hecate roadmap into Nobody context store (PostgreSQL)")
    parser.add_argument("--dry-run", action="store_true", help="Print node tree without inserting")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Context store URL (default: {DEFAULT_BASE_URL})")
    args = parser.parse_args()

    client = ContextStoreClient(args.base_url)

    if not args.dry_run:
        # Health check
        try:
            r = client.session.get(f"{args.base_url}/api/plans", timeout=5)
            r.raise_for_status()
        except Exception as e:
            print(f"Cannot reach context store at {args.base_url}: {e}", file=sys.stderr)
            print("Is the API running? Start with: cd context-store/Api && dotnet run", file=sys.stderr)
            sys.exit(1)

    seed(client, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
