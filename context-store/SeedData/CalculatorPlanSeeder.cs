// CodeStoragePoc - Calculator Plan Seeder
//
// Seeds a plan that describes a Calculator class, with tasks carrying structured
// code-generation attributes (method_name, return_type, params, body).
// PlanToCodeGenerator reads these attributes to produce code nodes — no free-text parsing.
//
// Node hierarchy:
//   plan "Calculator Library"
//   └── plan_phase "EXECUTE"
//       └── plan_step "Create Calculator class"
//           ├── task "Add method" { method_name: "Add", return_type: "int", params: "int a, int b", body: "return a + b;" }
//           ├── task "Subtract method" { method_name: "Subtract", ... }
//           └── task "Negate method" { method_name: "Negate", ... }
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class CalculatorPlanSeeder
{
    private readonly NodeRepository _repo;

    public Guid PlanId { get; private set; }
    public Guid ExecutePhaseId { get; private set; }
    public Guid CreateClassStepId { get; private set; }
    public Guid AddTaskId { get; private set; }
    public Guid SubtractTaskId { get; private set; }
    public Guid NegateTaskId { get; private set; }

    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public CalculatorPlanSeeder(NodeRepository repo) => _repo = repo;

    /// <summary>Seed the calculator plan tree. Returns the root plan node ID.</summary>
    public async Task<Guid> Seed(Guid projectId)
    {
        PlanId = Guid.NewGuid();
        await _repo.InsertNode(
            PlanId, projectId, null,
            "plan", "Calculator Library", "Generate a Calculator class from plan tasks",
            null, 0, "claude-opus-4-6",
            new()
            {
                { "level", "L1" },
                { "status", "in_progress" },
                { "plan_type", "feature" },
                { "target_namespace", "CodeStoragePoc.Generated" },
                { "target_class", "Calculator" },
            });
        _allNodeIds.Add(PlanId);

        // Single EXECUTE phase
        ExecutePhaseId = Guid.NewGuid();
        await _repo.InsertNode(
            ExecutePhaseId, projectId, null,
            "plan_phase", "EXECUTE", "Implement the Calculator class methods",
            PlanId, 100, "claude-opus-4-6",
            new() { { "status", "in_progress" } });
        _allNodeIds.Add(ExecutePhaseId);

        // Step: Create Calculator class
        CreateClassStepId = Guid.NewGuid();
        await _repo.InsertNode(
            CreateClassStepId, projectId, null,
            "plan_step", "Create Calculator class", "Static Calculator class with arithmetic methods",
            ExecutePhaseId, 100, "claude-opus-4-6",
            new() { { "status", "in_progress" } });
        _allNodeIds.Add(CreateClassStepId);

        // Task: Add method
        AddTaskId = Guid.NewGuid();
        await _repo.InsertNode(
            AddTaskId, projectId, null,
            "task", "Add method", "Two-argument integer addition",
            CreateClassStepId, 100, "claude-opus-4-6",
            new()
            {
                { "status", "pending" },
                { "method_name", "Add" },
                { "return_type", "int" },
                { "params", "int a, int b" },
                { "body", "return a + b;" },
            });
        _allNodeIds.Add(AddTaskId);

        // Task: Subtract method
        SubtractTaskId = Guid.NewGuid();
        await _repo.InsertNode(
            SubtractTaskId, projectId, null,
            "task", "Subtract method", "Two-argument integer subtraction",
            CreateClassStepId, 200, "claude-opus-4-6",
            new()
            {
                { "status", "pending" },
                { "method_name", "Subtract" },
                { "return_type", "int" },
                { "params", "int a, int b" },
                { "body", "return a - b;" },
            });
        _allNodeIds.Add(SubtractTaskId);

        // Task: Negate method
        NegateTaskId = Guid.NewGuid();
        await _repo.InsertNode(
            NegateTaskId, projectId, null,
            "task", "Negate method", "Single-argument negation",
            CreateClassStepId, 300, "claude-opus-4-6",
            new()
            {
                { "status", "pending" },
                { "method_name", "Negate" },
                { "return_type", "int" },
                { "params", "int a" },
                { "body", "return -a;" },
            });
        _allNodeIds.Add(NegateTaskId);

        Console.WriteLine($"[SEED]     Calculator plan seeded: {_allNodeIds.Count} nodes");
        return PlanId;
    }
}
