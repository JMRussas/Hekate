// CodeStoragePoc - Plan Seeder
//
// Inserts the Voice Ideation Assistant plan (001) as a node tree in PostgreSQL.
// This is the first test case for storing plans as nodes — the plan describes
// the system that will eventually manage plans.
//
// Node hierarchy:
//   plan
//   ├── plan_phase "GATHER"
//   ├── plan_phase "PLAN"
//   │   ├── plan_step (each numbered step)
//   │   │   └── task (sub-tasks)
//   │   ├── risk (each risk)
//   │   └── question (each open question)
//   ├── plan_phase "APPROVE"
//   ├── plan_phase "EXECUTE"
//   └── plan_phase "CLOSE_OUT"
//       ├── test_spec (each test)
//       └── retrospective
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class PlanSeeder
{
    private readonly NodeRepository _repo;

    // Expose IDs for graph edge creation and verification
    public Guid PlanId { get; private set; }
    public Guid GatherPhaseId { get; private set; }
    public Guid PlanPhaseId { get; private set; }
    public Guid ApprovePhaseId { get; private set; }
    public Guid ExecutePhaseId { get; private set; }
    public Guid CloseOutPhaseId { get; private set; }

    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public PlanSeeder(NodeRepository repo) => _repo = repo;

    /// <summary>Seed the full plan tree. Returns the root plan node ID.</summary>
    public async Task<Guid> Seed(Guid projectId)
    {
        PlanId = Guid.NewGuid();

        await _repo.InsertNode(
            PlanId, projectId, null,
            "plan", "Voice-Enabled Ideation Assistant", null,
            null, 0, "claude-opus-4-6",
            new()
            {
                { "level", "L2" },
                { "status", "plan" },
                { "plan_number", "001" },
                { "created_date", "2026-03-08" },
                { "origin", "Voice interaction design session (text-based ideation)" }
            });
        _allNodeIds.Add(PlanId);

        // --- Phases ---
        GatherPhaseId = await SeedPhase(projectId, PlanId, 100,
            "GATHER", "completed",
            "Read CodeStoragePoc codebase, Hekate extension, research voice AI landscape");

        PlanPhaseId = await SeedPhase(projectId, PlanId, 200,
            "PLAN", "completed",
            "Design unified node model, context router, voice pipeline, thread tracking");

        ApprovePhaseId = await SeedPhase(projectId, PlanId, 300,
            "APPROVE", "in_progress",
            "Present plan, get user approval before execution");

        ExecutePhaseId = await SeedPhase(projectId, PlanId, 400,
            "EXECUTE", "pending",
            "Implement schema extension, seeders, renderers, context router");

        CloseOutPhaseId = await SeedPhase(projectId, PlanId, 500,
            "CLOSE_OUT", "pending",
            "Run tests, update docs, write retrospective");

        // --- Steps under EXECUTE phase ---
        var step1 = await SeedStep(projectId, ExecutePhaseId, 100,
            "Schema Extension",
            "Add new node types and AGE edge labels for ideation and planning domains",
            "completed");

        var step2 = await SeedStep(projectId, ExecutePhaseId, 200,
            "Plan Seeder",
            "Build PlanSeeder.cs — inserts this plan as a node tree in PostgreSQL",
            "in_progress");

        var step3 = await SeedStep(projectId, ExecutePhaseId, 300,
            "Plan Renderer",
            "Build PlanRenderer.cs — reads plan from DB, renders as readable text",
            "pending");

        var step4 = await SeedStep(projectId, ExecutePhaseId, 400,
            "Program.cs Integration",
            "Wire plan seeding and rendering into the main demo loop",
            "pending");

        var step5 = await SeedStep(projectId, ExecutePhaseId, 500,
            "Context Router Service",
            "Define intent classifier and query assembler for per-model context payloads",
            "pending");

        var step6 = await SeedStep(projectId, ExecutePhaseId, 600,
            "Voice Layer Integration",
            "Gemini Live or Pipecat + local STT/TTS for voice input",
            "pending");

        var step7 = await SeedStep(projectId, ExecutePhaseId, 700,
            "Thread Tracking",
            "Idea lifecycle state machine, stale detection, resurfacing prompts",
            "pending");

        // --- Tasks under Context Router step ---
        await SeedTask(projectId, step5, 100,
            "Define intent classifier",
            "Classify user requests into ideation, planning, code review, question, etc.");

        await SeedTask(projectId, step5, 200,
            "Build query assembler",
            "Assemble per-model context from pgvector similarity, graph neighbors, thread status");

        await SeedTask(projectId, step5, 300,
            "Add context router endpoint",
            "New endpoint in Orchestration Engine that returns assembled context for a given intent");

        await SeedTask(projectId, step5, 400,
            "Wire Hekate chat",
            "Replace raw history passing with context router calls in chatView.ts");

        // --- Risks ---
        await SeedRisk(projectId, PlanId, 100,
            "AGE performance with many small Cypher queries",
            "Batch vertex creation instead of one-at-a-time MERGE",
            "medium");

        await SeedRisk(projectId, PlanId, 200,
            "Node type sprawl — 20+ types in one table",
            "Application-level validation, documented in CLAUDE.md",
            "low");

        await SeedRisk(projectId, PlanId, 300,
            "Cross-domain embedding dimension mismatch (1536 vs 768)",
            "Standardize on one dimension per project in the projects table",
            "medium");

        // --- Questions ---
        await SeedQuestion(projectId, PlanId, 100,
            "Embedding dimension: 1536 (OpenAI) vs 768 (nomic-embed-text)?",
            "Standardize on 768 for new voice/plan project, keep 1536 for code project",
            "proposed");

        await SeedQuestion(projectId, PlanId, 200,
            "File table: plans don't map to files. file_id NULL sufficient?",
            "Yes, file_id = NULL is fine for non-code domains",
            "proposed");

        // --- Test specs under CLOSE_OUT ---
        await SeedTestSpec(projectId, CloseOutPhaseId, 100,
            "PlanSeeder inserts correct node count with correct parent-child relationships",
            "unit");

        await SeedTestSpec(projectId, CloseOutPhaseId, 200,
            "PlanRenderer produces readable output matching original plan structure",
            "unit");

        await SeedTestSpec(projectId, CloseOutPhaseId, 300,
            "Round-trip: plan markdown → DB → rendered text → structural comparison",
            "integration");

        await SeedTestSpec(projectId, CloseOutPhaseId, 400,
            "AGE graph queries traverse from plan to idea to code nodes",
            "integration");

        // --- Retrospective placeholder ---
        var retroId = Guid.NewGuid();
        await _repo.InsertNode(
            retroId, projectId, null,
            "retrospective", "Plan 001 Retrospective", null,
            CloseOutPhaseId, 500, "claude-opus-4-6",
            new()
            {
                { "status", "pending" },
                { "template", "status|test_results|deviations|learnings|docs_updated" }
            });
        _allNodeIds.Add(retroId);

        Console.WriteLine($"[SEED]     Plan seeded: {_allNodeIds.Count} nodes");
        return PlanId;
    }

    private async Task<Guid> SeedPhase(Guid projectId, Guid parentId, int order,
        string name, string status, string description)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "plan_phase", name, description,
            parentId, order, "claude-opus-4-6",
            new() { { "status", status } });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedStep(Guid projectId, Guid parentId, int order,
        string name, string description, string status)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "plan_step", name, description,
            parentId, order, "claude-opus-4-6",
            new() { { "status", status } });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedTask(Guid projectId, Guid parentId, int order,
        string name, string description)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "task", name, description,
            parentId, order, "claude-opus-4-6",
            new() { { "status", "pending" } });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedRisk(Guid projectId, Guid parentId, int order,
        string description, string mitigation, string severity)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "risk", null, description,
            parentId, order, "claude-opus-4-6",
            new()
            {
                { "mitigation", mitigation },
                { "severity", severity }
            });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedQuestion(Guid projectId, Guid parentId, int order,
        string questionText, string proposedAnswer, string status)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "question", null, questionText,
            parentId, order, "claude-opus-4-6",
            new()
            {
                { "proposed_answer", proposedAnswer },
                { "status", status }
            });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedTestSpec(Guid projectId, Guid parentId, int order,
        string description, string testType)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "test_spec", null, description,
            parentId, order, "claude-opus-4-6",
            new()
            {
                { "test_type", testType },
                { "status", "pending" }
            });
        _allNodeIds.Add(id);
        return id;
    }
}
