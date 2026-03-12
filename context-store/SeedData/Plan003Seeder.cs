// CodeStoragePoc - Plan 003 Seeder
//
// Inserts Plan 003 (Review Fixes — Hardening Pass) as a node tree.
// Fixes critical bugs and security issues from consolidated Claude + Gemini review.
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class Plan003Seeder
{
    private readonly NodeRepository _repo;

    public Guid PlanId { get; private set; }
    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public Plan003Seeder(NodeRepository repo) => _repo = repo;

    public async Task<Guid> Seed(Guid projectId)
    {
        PlanId = Guid.NewGuid();

        await _repo.InsertNode(
            PlanId, projectId, null,
            "plan", "Review Fixes — Hardening Pass", null,
            null, 0, "claude-opus-4-6",
            new()
            {
                { "level", "L3" },
                { "status", "approve" },
                { "plan_number", "003" },
                { "created_date", "2026-03-08" },
                { "depends_on", "002" },
                { "origin", "Consolidated Claude + Gemini review (research/003-consolidated-review.md)" }
            });
        _allNodeIds.Add(PlanId);

        // Phases
        var gather = await SeedPhase(projectId, 100, "GATHER", "completed",
            "Claude + Gemini code review of all source files, documented in research/003");

        var plan = await SeedPhase(projectId, 200, "PLAN", "completed",
            "L3 plan with security analysis, 8 steps, risk assessment");

        var approve = await SeedPhase(projectId, 300, "APPROVE", "in_progress",
            "Present plan to user for approval before execution");

        var execute = await SeedPhase(projectId, 400, "EXECUTE", "pending",
            "Fix critical bugs, security issues, and design gaps");

        var closeOut = await SeedPhase(projectId, 500, "CLOSE_OUT", "pending",
            "Build, run full demo, update docs, write retrospective");

        // Steps under EXECUTE
        await SeedStep(projectId, execute, 100,
            "Fix advisory lock session mismatch",
            "Add optional NpgsqlConnection param to NodeRepository mutation methods. " +
            "SubtreeLock exposes its connection. Program.cs passes lock connection to mutations.",
            "pending");

        await SeedStep(projectId, execute, 200,
            "Fix lock release ordering",
            "Move ReleaseSubtree in Program.cs to after code regeneration and validation.",
            "pending");

        await SeedStep(projectId, execute, 300,
            "Add transaction support",
            "NodeRepository.InsertNode wraps node + attribute inserts in transaction. " +
            "Add BeginTransaction helper for multi-step mutations.",
            "pending");

        await SeedStep(projectId, execute, 400,
            "Fix Cypher injection",
            "Add EdgeType whitelist to AgeLayer. Validate before query construction. " +
            "Improve string escaping for node names (backslash + single quote).",
            "pending");

        await SeedStep(projectId, execute, 500,
            "Move credentials to environment variable",
            "Program.cs reads CODESTORAGE_CONNSTR env var with POC fallback and warning.",
            "pending");

        await SeedStep(projectId, execute, 600,
            "Parallelize context assembly",
            "ContextAssembler.Assemble() uses Task.WhenAll for independent queries per intent.",
            "pending");

        await SeedStep(projectId, execute, 700,
            "Add node type constants",
            "Create NodeTypes.cs with string constants for all known node types. " +
            "Use throughout ContextAssembler and seeders.",
            "pending");

        await SeedStep(projectId, execute, 800,
            "Build and verify full demo",
            "dotnet build (0 errors), dotnet run (all 13 sections pass).",
            "pending");

        // Risks
        await SeedNode(projectId, PlanId, 600, "risk", null,
            "AGE Cypher doesn't support true parameterized queries — whitelisting is pragmatic fix",
            new() { { "mitigation", "Whitelist valid edge types, escape string values" }, { "severity", "medium" } });

        await SeedNode(projectId, PlanId, 700, "risk", null,
            "Optional connection parameter pattern — callers could accidentally skip it",
            new() { { "mitigation", "Document pattern, SubtreeLock enforces via API" }, { "severity", "low" } });

        await SeedNode(projectId, PlanId, 800, "risk", null,
            "NodeRepository API changes affect all callers (seeders, Program.cs, ContextAssembler)",
            new() { { "mitigation", "Optional params — existing callers unchanged. All must compile." }, { "severity", "medium" } });

        // Questions
        await SeedNode(projectId, PlanId, 900, "question", null,
            "Should we also add NpgsqlDataSource (Npgsql 8+ connection pooling) in this pass?",
            new() { { "proposed_answer", "Defer — performance improvement, not correctness fix. Keep plan focused." }, { "status", "proposed" } });

        await SeedNode(projectId, PlanId, 1000, "question", null,
            "Should NodeTypes constants be an enum or static string class?",
            new() { { "proposed_answer", "Static string class — matches DB TEXT column, avoids conversion overhead." }, { "status", "proposed" } });

        // Test specs
        await SeedNode(projectId, closeOut, 100, "test_spec", null,
            "dotnet build — 0 errors, 0 warnings",
            new() { { "test_type", "build" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 200, "test_spec", null,
            "dotnet run — all 13 demo sections pass including agent navigation and mutation",
            new() { { "test_type", "integration" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 300, "test_spec", null,
            "Lock session: mutation demo uses lock connection (verify via console output)",
            new() { { "test_type", "integration" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 400, "test_spec", null,
            "Cypher injection: invalid edge type throws ArgumentException",
            new() { { "test_type", "unit" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 500, "test_spec", null,
            "Credentials: CODESTORAGE_CONNSTR env var used when set, fallback with warning when unset",
            new() { { "test_type", "manual" }, { "status", "pending" } });

        // Retrospective placeholder
        await SeedNode(projectId, closeOut, 600, "retrospective", "Plan 003 Retrospective", null,
            new() { { "status", "pending" } });

        Console.WriteLine($"[SEED]     Plan 003 seeded: {_allNodeIds.Count} nodes");
        return PlanId;
    }

    private async Task<Guid> SeedPhase(Guid projectId, int order,
        string name, string status, string description)
    {
        return await SeedNode(projectId, PlanId, order, "plan_phase", name, description,
            new() { { "status", status } });
    }

    private async Task<Guid> SeedStep(Guid projectId, Guid parentId, int order,
        string name, string description, string status)
    {
        return await SeedNode(projectId, parentId, order, "plan_step", name, description,
            new() { { "status", status } });
    }

    private async Task<Guid> SeedNode(Guid projectId, Guid parentId, int order,
        string nodeType, string? name, string? value,
        Dictionary<string, string>? attributes = null)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            nodeType, name, value,
            parentId, order, "claude-opus-4-6",
            attributes);
        _allNodeIds.Add(id);
        return id;
    }
}
