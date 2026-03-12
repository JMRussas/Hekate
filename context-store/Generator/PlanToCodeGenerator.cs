// CodeStoragePoc - Plan-to-Code Generator
//
// Reads task nodes from a plan and creates code nodes (compilation_unit, namespace,
// class, methods, parameters, blocks, statements). Links each task to its generated
// method via IMPLEMENTED_BY temporal edges.
//
// DESIGN DECISIONS:
// - Task attributes (method_name, return_type, params, body) are structured, not free-text.
//   PlanToCodeGenerator does NOT parse natural language descriptions.
// - The plan root carries target_namespace and target_class attributes.
// - IMPLEMENTED_BY edges are temporal (valid_from/valid_to) so we can track which
//   version of a task produced which version of the code.
// - On regeneration, old code nodes are deleted and old edges are closed.
//
// Depends on: NodeRepository, AgeLayer
// Used by:    Program

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.GraphLayer;

namespace CodeStoragePoc.Generator;

public class PlanToCodeGenerator
{
    private readonly NodeRepository _repo;
    private readonly AgeLayer _age;
    private readonly bool _ageAvailable;

    // Maps task ID → generated method node ID (for edge creation and queries)
    private readonly Dictionary<Guid, Guid> _taskToMethod = new();
    public IReadOnlyDictionary<Guid, Guid> TaskToMethodMap => _taskToMethod;

    // All generated code node IDs (for cleanup tracking)
    private readonly List<Guid> _generatedNodeIds = new();
    public IReadOnlyList<Guid> GeneratedNodeIds => _generatedNodeIds;

    public Guid RootNodeId { get; private set; }

    public PlanToCodeGenerator(NodeRepository repo, AgeLayer age, bool ageAvailable = true)
    {
        _repo = repo;
        _age = age;
        _ageAvailable = ageAvailable;
    }

    /// <summary>
    /// Generate code nodes from a plan. Reads the plan root for target_namespace
    /// and target_class, then finds all task children with code-gen attributes.
    /// Creates code nodes and IMPLEMENTED_BY edges.
    /// Returns the root code node ID (compilation_unit).
    /// </summary>
    public async Task<Guid> GenerateFromPlan(Guid planId, Guid projectId, Guid fileId)
    {
        // Read plan root for class/namespace config
        var planNode = await _repo.GetNodeWithAttributes(planId);
        if (planNode == null)
            throw new InvalidOperationException($"Plan {planId} not found");

        var targetNamespace = planNode.Attr("target_namespace", "Generated");
        var targetClass = planNode.Attr("target_class", "GeneratedClass");

        // Find all task nodes under this plan (recursive)
        var tasks = await FindTasksWithCodeAttrs(planId);
        if (tasks.Count == 0)
            throw new InvalidOperationException("Plan has no tasks with code-gen attributes");

        // Build the code node tree
        RootNodeId = await BuildCodeTree(projectId, fileId, targetNamespace, targetClass, tasks);

        // Create IMPLEMENTED_BY edges (task → method)
        if (_ageAvailable)
        {
            // Sync BOTH plan and code vertices — tasks live in the plan project,
            // code nodes live in the code project. Both must be AGE vertices
            // before we can create edges between them.
            var planProjectId = planNode.Record.ProjectId;
            await _age.SyncAllVertices(_repo, planProjectId);
            await _age.SyncAllVertices(_repo, projectId);

            foreach (var (taskId, methodId) in _taskToMethod)
            {
                await _age.CreateTemporalEdge(taskId, methodId, "IMPLEMENTED_BY", "plan-to-code");
            }
        }

        return RootNodeId;
    }

    /// <summary>
    /// Regenerate code from a modified plan. Closes old IMPLEMENTED_BY edges,
    /// deletes old code nodes, creates new ones.
    /// </summary>
    public async Task<Guid> RegenerateFromPlan(Guid planId, Guid projectId, Guid fileId)
    {
        // Close old IMPLEMENTED_BY edges from all tasks
        if (_ageAvailable)
        {
            var oldTasks = await FindTasksWithCodeAttrs(planId);
            foreach (var task in oldTasks)
            {
                await _age.CloseAllEdgesFrom(task.Record.Id, "IMPLEMENTED_BY");
            }
        }

        // Delete old code nodes for this file
        await _repo.DeleteNodesByFile(fileId);

        // Clear tracking state
        _taskToMethod.Clear();
        _generatedNodeIds.Clear();

        // Regenerate
        return await GenerateFromPlan(planId, projectId, fileId);
    }

    /// <summary>
    /// Find all task nodes under a plan that have code-gen attributes.
    /// Walks the tree recursively via GetChildren.
    /// </summary>
    private async Task<List<TreeNode>> FindTasksWithCodeAttrs(Guid rootId)
    {
        var result = new List<TreeNode>();
        await WalkForTasks(rootId, result);
        return result;
    }

    private async Task WalkForTasks(Guid parentId, List<TreeNode> result)
    {
        var children = await _repo.GetChildren(parentId);
        foreach (var child in children)
        {
            if (child.Record.NodeType == "task" && child.Attributes.ContainsKey("method_name"))
            {
                result.Add(child);
            }
            // Recurse into phases, steps
            await WalkForTasks(child.Record.Id, result);
        }
    }

    /// <summary>
    /// Build the full code node tree: compilation_unit → namespace → class → methods.
    /// </summary>
    private async Task<Guid> BuildCodeTree(
        Guid projectId, Guid fileId,
        string targetNamespace, string targetClass,
        List<TreeNode> tasks)
    {
        // compilation_unit (root)
        var rootId = Guid.NewGuid();
        await InsertCodeNode(rootId, projectId, fileId, null, "compilation_unit", null, null, 0);

        // using System;
        var usingId = Guid.NewGuid();
        await InsertCodeNode(usingId, projectId, fileId, rootId, "using_directive", "System", null, 100);

        // namespace
        var nsId = Guid.NewGuid();
        await InsertCodeNode(nsId, projectId, fileId, rootId, "namespace", targetNamespace, null, 200);

        // class
        var classId = Guid.NewGuid();
        await InsertCodeNode(classId, projectId, fileId, nsId, "class", targetClass, null, 100,
            new() { { "access", "public" }, { "modifier", "static" } });

        // methods from tasks
        int methodOrder = 100;
        foreach (var task in tasks)
        {
            var methodName = task.Attr("method_name");
            var returnType = task.Attr("return_type", "void");
            var paramsStr = task.Attr("params", "");
            var body = task.Attr("body", "");

            // method node
            var methodId = Guid.NewGuid();
            await InsertCodeNode(methodId, projectId, fileId, classId, "method", methodName, null, methodOrder,
                new() { { "access", "public" }, { "modifier", "static" }, { "return_type", returnType } });

            // parse params: "int a, int b" → parameter nodes
            int paramOrder = 100;
            if (!string.IsNullOrEmpty(paramsStr))
            {
                foreach (var param in paramsStr.Split(',', StringSplitOptions.TrimEntries))
                {
                    var parts = param.Split(' ', 2, StringSplitOptions.TrimEntries);
                    if (parts.Length == 2)
                    {
                        var paramId = Guid.NewGuid();
                        await InsertCodeNode(paramId, projectId, fileId, methodId, "parameter", parts[1], null, paramOrder,
                            new() { { "type", parts[0] } });
                        paramOrder += 100;
                    }
                }
            }

            // block → statement
            var blockId = Guid.NewGuid();
            await InsertCodeNode(blockId, projectId, fileId, methodId, "block", null, null, paramOrder);

            var stmtId = Guid.NewGuid();
            await InsertCodeNode(stmtId, projectId, fileId, blockId, "statement", null, body, 100);

            // Track task → method mapping
            _taskToMethod[task.Record.Id] = methodId;

            methodOrder += 100;
        }

        return rootId;
    }

    private async Task InsertCodeNode(
        Guid id, Guid projectId, Guid fileId, Guid? parentId,
        string nodeType, string? name, string? value, int siblingOrder,
        Dictionary<string, string>? attrs = null)
    {
        await _repo.InsertNode(id, projectId, fileId, nodeType, name, value,
            parentId, siblingOrder, "plan-to-code", attrs);
        _generatedNodeIds.Add(id);
    }
}
