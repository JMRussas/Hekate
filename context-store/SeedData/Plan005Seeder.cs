// CodeStoragePoc - Plan 005 Seeder
//
// Seeds the Planner View plan with enriched contract attributes:
// plan_type, priority, target_date, severity on risks, blockers, milestones.
// Exercises all new node types from the expanded planning contract.
//
// Node hierarchy:
//   plan (plan_type=feature, priority=p1, target_date)
//   ├── milestone "Plan contract validated"
//   ├── milestone "API serving plan data"
//   ├── plan_phase "GATHER"
//   ├── plan_phase "PLAN"
//   ├── plan_phase "EXECUTE"
//   │   ├── plan_step "Add node types"
//   │   │   └── task
//   │   ├── plan_step "Build plan API"
//   │   │   ├── task
//   │   │   ├── question
//   │   │   └── blocker
//   │   ├── plan_step "Build planner UI"
//   │   │   ├── task
//   │   │   └── risk (severity=medium)
//   │   └── plan_step "Seed enriched plan"
//   ├── plan_phase "CLOSE_OUT"
//   │   ├── test_spec
//   │   └── retrospective
//   ├── risk (severity=high, abort_trigger)
//   ├── decision
//   └── question
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class Plan005Seeder
{
    private readonly NodeRepository _repo;

    public Guid PlanId { get; private set; }
    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public Plan005Seeder(NodeRepository repo) => _repo = repo;

    // Helper: InsertNode signature is (id, projectId, fileId, nodeType, name, value, parentId, siblingOrder, modifiedBy, attributes)
    // Plans have fileId=null always. parentId is the tree parent.
    private async Task<Guid> Add(Guid projectId, Guid? parentId, int order,
        string nodeType, string name, string? value, Dictionary<string, string>? attrs)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(id, projectId, null, nodeType, name, value, parentId, order, null, attrs);
        _allNodeIds.Add(id);
        return id;
    }

    public async Task<Guid> Seed(Guid projectId)
    {
        PlanId = Guid.NewGuid();

        // --- Root plan ---
        await _repo.InsertNode(
            PlanId, projectId, null,
            "plan", "Planner View", "Add plan contract, API endpoints, and Planner panel to the ideation assistant UI",
            null, 0, "claude-opus-4-6",
            new()
            {
                { "level", "L2" },
                { "status", "completed" },
                { "plan_number", "005" },
                { "plan_type", "feature" },
                { "priority", "p1" },
                { "target_date", "2026-03-09" },
                { "created_date", "2026-03-08" },
                { "origin", "Planner view design session — dual Claude + Gemini review" }
            });
        _allNodeIds.Add(PlanId);

        // --- Milestones (plan-level) ---
        await Add(projectId, PlanId, 50, "milestone", "Plan contract validated",
            "Dual review complete, contract finalized",
            new() { { "status", "completed" } });

        await Add(projectId, PlanId, 60, "milestone", "API serving plan data",
            "GET /api/plans and /api/plan/{id} returning data",
            new() { { "status", "completed" } });

        // --- Phases ---
        await Add(projectId, PlanId, 100, "plan_phase", "GATHER", null,
            new() { { "status", "completed" } });

        await Add(projectId, PlanId, 200, "plan_phase", "PLAN", null,
            new() { { "status", "completed" } });

        var approve = await Add(projectId, PlanId, 300, "plan_phase", "APPROVE", null,
            new() { { "status", "completed" } });

        await Add(projectId, approve, 100, "decision", "User approved plan",
            "Plan reviewed and approved for execution",
            new() { { "status", "committed" } });

        var execute = await Add(projectId, PlanId, 400, "plan_phase", "EXECUTE", null,
            new() { { "status", "completed" } });

        // --- Steps under EXECUTE ---

        // Step 1: Add node types
        var step1 = await Add(projectId, execute, 100, "plan_step", "Add new node types to schema", null,
            new() { { "status", "completed" } });

        await Add(projectId, step1, 100, "task", "Add Blocker and Milestone to NodeTypes.cs", null,
            new() { { "status", "completed" } });

        // Step 2: Build plan API
        var step2 = await Add(projectId, execute, 200, "plan_step", "Build plan API endpoints", null,
            new() { { "status", "completed" } });

        await Add(projectId, step2, 100, "task", "Create PlanService.cs with ListPlans and GetPlanTree", null,
            new() { { "status", "completed" } });

        await Add(projectId, step2, 200, "task", "Add GET /api/plans and GET /api/plan/{id} endpoints", null,
            new() { { "status", "completed" } });

        await Add(projectId, step2, 300, "question", "REST or reuse /threads pattern?", null,
            new() { { "status", "proposed" }, { "proposed_answer", "Dedicated /api/plans and /api/plan/{id} — plans are project-scoped, threads are conversation-scoped" } });

        await Add(projectId, step2, 400, "blocker", "API process locks DLL during build",
            "Must kill Api.exe before rebuilding",
            new() { { "status", "resolved" }, { "owner", "dev-mcp build tool" } });

        // Step 3: Build planner UI
        var step3 = await Add(projectId, execute, 300, "plan_step", "Build Planner UI panel", null,
            new() { { "status", "completed" } });

        await Add(projectId, step3, 100, "task", "Create PlannerPanel.tsx with list and tree views", null,
            new() { { "status", "completed" } });

        await Add(projectId, step3, 200, "task", "Wire Planner tab into App.tsx right panel", null,
            new() { { "status", "completed" } });

        await Add(projectId, step3, 300, "risk", "Tree depth rendering in 256px panel",
            "Plan trees 4+ levels deep may clip at 12px indent per level",
            new() { { "severity", "medium" }, { "mitigation", "Truncate names, compact indent, add horizontal scroll if needed" } });

        // Step 4: Seed enriched plan
        var step4 = await Add(projectId, execute, 400, "plan_step", "Seed enriched plan with new types", null,
            new() { { "status", "completed" } });

        await Add(projectId, step4, 100, "task", "Create Plan005Seeder with blockers, milestones, severity", null,
            new() { { "status", "completed" } });

        // --- CLOSE_OUT phase ---
        var closeOut = await Add(projectId, PlanId, 500, "plan_phase", "CLOSE_OUT", null,
            new() { { "status", "completed" } });

        await Add(projectId, closeOut, 100, "test_spec", "Playwright: Planner tab renders plan list", null,
            new() { { "test_type", "integration" }, { "status", "completed" } });

        await Add(projectId, closeOut, 200, "test_spec", "Playwright: Plan tree expands with correct types", null,
            new() { { "test_type", "integration" }, { "status", "completed" } });

        await Add(projectId, closeOut, 300, "retrospective", "Plan 005 Retrospective", null,
            new() { { "status", "completed" }, { "template", "status, test results, deviations, learnings, docs updated" } });

        // --- Plan-level risk ---
        await Add(projectId, PlanId, 600, "risk", "Scope creep from plan taxonomy",
            "Adding too many node types before usage validates them",
            new() { { "severity", "high" }, { "abort_trigger", "true" }, { "mitigation", "Ship 12 types, defer key_result/assumption/constraint until real demand" } });

        // --- Plan-level decision ---
        await Add(projectId, PlanId, 700, "decision", "Dropped objective node",
            "Plan node itself IS the objective — name/value carry it. One plan = one goal.",
            new() { { "status", "committed" } });

        // --- Plan-level question ---
        await Add(projectId, PlanId, 800, "question", "Should Planner be a wider panel or right-tab?", null,
            new() { { "status", "proposed" }, { "proposed_answer", "Start as right-panel tab (256px). Promote to wider if trees feel cramped." } });

        return PlanId;
    }
}
