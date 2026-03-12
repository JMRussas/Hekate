// CodeStoragePoc - Edge Seeder
//
// Scans decomposed code nodes and creates AGE graph edges for
// code relationships: CALLS (method invocations) and REFERENCES
// (field/property access). Runs after decomposition.
//
// Strategy: for each method/constructor, scan statement text for
// identifiers matching sibling field/method names. Simple heuristic
// (not semantic analysis) — good enough for POC.
//
// Depends on: DbLayer/NodeRepository, GraphLayer/AgeLayer
// Used by:    Api/Services/CodeService

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.GraphLayer;

namespace CodeStoragePoc.Decomposer;

public class EdgeSeeder
{
    private readonly NodeRepository _repo;
    private readonly AgeLayer _age;

    public EdgeSeeder(NodeRepository repo, AgeLayer age)
    {
        _repo = repo;
        _age = age;
    }

    /// <summary>
    /// Scan all code nodes in a file and create CALLS/REFERENCES edges.
    /// Must be called after decomposition and AGE vertex sync.
    /// </summary>
    public async Task<(int calls, int refs)> SeedEdgesForFile(Guid fileId)
    {
        var nodes = await _repo.GetNodesByFile(fileId);
        if (nodes.Count == 0) return (0, 0);

        // Build lookup of names → node IDs for fields and methods within each type
        var typeNodes = nodes.Where(n => n.Record.NodeType is "class" or "struct").ToList();
        int totalCalls = 0, totalRefs = 0;

        foreach (var typeNode in typeNodes)
        {
            var members = nodes.Where(n => n.Record.ParentId == typeNode.Record.Id).ToList();
            var fields = members.Where(n => n.Record.NodeType == "field").ToList();
            var methods = members.Where(n => n.Record.NodeType is "method" or "constructor").ToList();

            // For each method/constructor, collect all statement text
            foreach (var method in methods)
            {
                var statements = CollectStatements(nodes, method.Record.Id);
                var bodyText = string.Join("\n", statements);

                if (string.IsNullOrWhiteSpace(bodyText)) continue;

                // Check for field references
                foreach (var field in fields)
                {
                    if (field.Record.Name != null && bodyText.Contains(field.Record.Name))
                    {
                        try
                        {
                            await _age.CreateEdge(method.Record.Id, field.Record.Id, "REFERENCES");
                            totalRefs++;
                        }
                        catch { /* Edge may already exist or AGE unavailable */ }
                    }
                }

                // Check for method calls (other methods in the same type)
                foreach (var other in methods)
                {
                    if (other.Record.Id == method.Record.Id) continue;
                    if (other.Record.Name != null && bodyText.Contains(other.Record.Name + "("))
                    {
                        try
                        {
                            await _age.CreateEdge(method.Record.Id, other.Record.Id, "CALLS");
                            totalCalls++;
                        }
                        catch { /* Edge may already exist or AGE unavailable */ }
                    }

                    // Constructor calls: "new TypeName("
                    if (other.Record.NodeType == "constructor")
                    {
                        var typeName = other.Attr("parent_type_name");
                        if (!string.IsNullOrEmpty(typeName) && bodyText.Contains($"new {typeName}("))
                        {
                            try
                            {
                                await _age.CreateEdge(method.Record.Id, other.Record.Id, "CALLS");
                                totalCalls++;
                            }
                            catch { }
                        }
                    }
                }
            }
        }

        return (totalCalls, totalRefs);
    }

    /// <summary>Collect all statement values under a method/constructor (via block children).</summary>
    private static List<string> CollectStatements(List<TreeNode> allNodes, Guid parentId)
    {
        var results = new List<string>();
        var children = allNodes.Where(n => n.Record.ParentId == parentId).ToList();

        foreach (var child in children)
        {
            if (child.Record.NodeType == "statement" && child.Record.Value != null)
                results.Add(child.Record.Value);
            else if (child.Record.NodeType == "block")
                results.AddRange(CollectStatements(allNodes, child.Record.Id));
        }

        return results;
    }
}
