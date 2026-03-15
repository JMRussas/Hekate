// CodeStoragePoc - Plan 002 Seeder
//
// Inserts Plan 002 (Ideation Nodes & Context Router) as a node tree.
// Uses the same PlanSeeder pattern but for the second plan.
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class Plan002Seeder
{
    private readonly NodeRepository _repo;

    public Guid PlanId { get; private set; }
    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public Plan002Seeder(NodeRepository repo) => _repo = repo;

    public async Task<Guid> Seed(Guid projectId)
    {
        PlanId = Guid.NewGuid();

        await _repo.InsertNode(
            PlanId, projectId, null,
            "plan", "Ideation Nodes & Context Router", null,
            null, 0, "claude-opus-4-6",
            new()
            {
                { "level", "L2" },
                { "status", "execute" },
                { "plan_number", "002" },
                { "created_date", "2026-03-08" },
                { "depends_on", "001" },
                { "origin", "Follow-on from Plan 001 — transport-agnostic conversation pipeline" }
            });
        _allNodeIds.Add(PlanId);

        // Phases
        var gather = await SeedPhase(projectId, 100, "GATHER", "completed",
            "Read CodeStoragePoc codebase, Hekate chat implementation, Plan 001 output");

        var plan = await SeedPhase(projectId, 200, "PLAN", "completed",
            "Design transport-agnostic pipeline, context router architecture, intent classification");

        var approve = await SeedPhase(projectId, 300, "APPROVE", "completed",
            "User approved modular voice/text approach with shared pipeline");

        var execute = await SeedPhase(projectId, 400, "EXECUTE", "in_progress",
            "Build conversation seeder, renderers, context router components");

        var closeOut = await SeedPhase(projectId, 500, "CLOSE_OUT", "pending",
            "Run tests, update docs, write retrospective");

        // Steps under EXECUTE
        await SeedStep(projectId, execute, 100,
            "ConversationSeeder",
            "Seed sample conversation with voice+text turns, topics, ideas, decisions",
            "completed");

        await SeedStep(projectId, execute, 200,
            "ConversationRenderer",
            "Render conversation tree as threaded transcript with speaker/mode badges",
            "completed");

        await SeedStep(projectId, execute, 300,
            "IntentClassifier",
            "Pattern-based intent detection: ideation, deepening, planning, recalling, parking, etc.",
            "completed");

        await SeedStep(projectId, execute, 400,
            "ContextAssembler",
            "DB queries per intent — returns curated context, not raw history",
            "completed");

        await SeedStep(projectId, execute, 500,
            "PromptBuilder",
            "Assemble XML-structured prompts with role, context, and user input",
            "completed");

        await SeedStep(projectId, execute, 600,
            "Program.cs Integration",
            "Wire conversation + context router demo, prove same input → different contexts by intent",
            "in_progress");

        // Risks
        await SeedNode(projectId, PlanId, 600, "risk", null,
            "Over-extraction: Claude might extract too many ideas from casual conversation",
            new() { { "mitigation", "Confidence threshold attribute, only surface high-confidence" }, { "severity", "medium" } });

        await SeedNode(projectId, PlanId, 700, "risk", null,
            "Intent misclassification: pattern matching may be too rigid",
            new() { { "mitigation", "Default to ideation (broadest context), add model fallback later" }, { "severity", "low" } });

        // Questions
        await SeedNode(projectId, PlanId, 800, "question", null,
            "Start with pattern matching or jump to model-based intent classification?",
            new() { { "proposed_answer", "Pattern-based first. Fast, deterministic, no API cost." }, { "status", "resolved" } });

        await SeedNode(projectId, PlanId, 900, "question", null,
            "Extract ideas synchronously after each turn, or batch after conversation ends?",
            new() { { "proposed_answer", "After each model response. Keeps ideas current for mid-conversation context." }, { "status", "resolved" } });

        // Test specs
        await SeedNode(projectId, closeOut, 100, "test_spec", null,
            "ConversationSeeder creates correct node/edge structure",
            new() { { "test_type", "unit" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 200, "test_spec", null,
            "IntentClassifier detects known patterns (ideation, parking, recalling)",
            new() { { "test_type", "unit" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 300, "test_spec", null,
            "ContextAssembler returns different payloads for different intents on same conversation",
            new() { { "test_type", "unit" }, { "status", "pending" } });

        await SeedNode(projectId, closeOut, 400, "test_spec", null,
            "Full pipeline: seed → classify → assemble → build prompt → verify relevant context",
            new() { { "test_type", "integration" }, { "status", "pending" } });

        // Retrospective placeholder
        await SeedNode(projectId, closeOut, 500, "retrospective", "Plan 002 Retrospective", null,
            new() { { "status", "pending" } });

        Console.WriteLine($"[SEED]     Plan 002 seeded: {_allNodeIds.Count} nodes");
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
