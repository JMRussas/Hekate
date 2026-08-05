// CodeStoragePoc - Semantic Edge Resolver
//
// Cross-file semantic edge resolution via Roslyn. Replaces the body-text
// approach in EdgeSeeder for project-wide call/reference graphs.
//
// STRATEGY:
// 1. List every file in the project; materialize source via CSharpGenerator
//    (the node tree is the source of truth — we round-trip it back to C#).
// 2. Build ONE CSharpCompilation holding all SyntaxTrees + AppDomain metadata
//    references. Symbols defined in any file are visible from any other tree.
// 3. Build a (type-full-name, member-name, arity) -> node_id lookup from the
//    persisted decomposer output. Walk the in-memory tree to assemble each
//    type's fully-qualified name (including nested types / nested namespaces).
// 4. For each declared method/constructor: walk its body for invocations,
//    object creations, member-accesses, and bare identifier references.
//    Resolve each via SemanticModel -> ISymbol; map back to a node_id via
//    the lookup; create CALLS / REFERENCES edges through AgeLayer.
//
// NOT touched: /api/code/decompose continues to call EdgeSeeder for the
// intra-file heuristic edges. This resolver augments with cross-file edges.
//
// Vertices are NOT re-synced — they already exist from decompose. We only
// add edges. SyncAllVertices is O(N) per node, so calling it here would
// re-burn ~10min on a 8k-node project for no benefit.
//
// Depends on: DbLayer/NodeRepository, Generator/CSharpGenerator,
//             GraphLayer/AgeLayer, Microsoft.CodeAnalysis.CSharp
// Used by:    Api/Services/CodeService.RebuildSemanticEdges

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.Generator;
using CodeStoragePoc.GraphLayer;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Npgsql;

namespace CodeStoragePoc.Decomposer;

public class SemanticEdgeResolver
{
    private readonly NodeRepository _repo;
    private readonly AgeLayer _age;
    private readonly string _connStr;

    public SemanticEdgeResolver(NodeRepository repo, AgeLayer age, string connStr)
    {
        _repo = repo;
        _age = age;
        _connStr = connStr;
    }

    public async Task<RebuildEdgesResult> RebuildForProject(Guid projectId)
    {
        var files = await ListProjectFiles(projectId);
        if (files.Count == 0)
            return new RebuildEdgesResult(0, 0, 0, 0, "no files in project");

        // Step 1: materialize source + parse each file.
        var generator = new CSharpGenerator(_repo);
        var trees = new List<SyntaxTree>();
        var memberLookup = new Dictionary<string, Guid>(StringComparer.Ordinal);
        var fieldLookup = new Dictionary<string, Guid>(StringComparer.Ordinal);

        int filesProcessed = 0;
        int filesSkipped = 0;

        foreach (var file in files)
        {
            try
            {
                var source = await generator.GenerateFromFile(file.Id);
                if (string.IsNullOrWhiteSpace(source))
                {
                    filesSkipped++;
                    continue;
                }
                var tree = CSharpSyntaxTree.ParseText(
                    source,
                    new CSharpParseOptions(LanguageVersion.Latest),
                    path: file.Path);
                trees.Add(tree);

                var nodes = await _repo.GetNodesByFile(file.Id);
                IndexFileNodes(nodes, memberLookup, fieldLookup);
                filesProcessed++;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[REBUILD]  Skipped {file.Path}: {ex.Message}");
                filesSkipped++;
            }
        }

        if (trees.Count == 0)
            return new RebuildEdgesResult(filesProcessed, filesSkipped, 0, 0, "no parseable files");

        // Step 2: compilation with whatever metadata is loaded in this process.
        // Private types from the project being indexed (NoZ.* etc.) will not be
        // present — that's fine, we only care about edges between user-defined
        // methods, all of which live in the SyntaxTrees.
        var refs = new List<MetadataReference>();
        foreach (var a in AppDomain.CurrentDomain.GetAssemblies())
        {
            if (a.IsDynamic) continue;
            if (string.IsNullOrEmpty(a.Location)) continue;
            try { refs.Add(MetadataReference.CreateFromFile(a.Location)); }
            catch { /* skip unreadable assemblies */ }
        }

        var compilation = CSharpCompilation.Create(
            "ContextStoreRebuild_" + projectId.ToString("N"),
            trees,
            refs,
            new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));

        // Step 3: walk every declared method/constructor and resolve refs.
        int callsCreated = 0;
        int refsCreated = 0;
        var edgeDedup = new HashSet<string>(StringComparer.Ordinal);

        foreach (var tree in trees)
        {
            var model = compilation.GetSemanticModel(tree, ignoreAccessibility: true);
            var root = tree.GetCompilationUnitRoot();

            foreach (var memberDecl in root.DescendantNodes().OfType<BaseMethodDeclarationSyntax>())
            {
                var sourceSymbol = model.GetDeclaredSymbol(memberDecl) as IMethodSymbol;
                if (sourceSymbol == null) continue;

                var sourceKey = BuildMemberKey(sourceSymbol);
                if (sourceKey == null) continue;
                if (!memberLookup.TryGetValue(sourceKey, out var sourceNodeId))
                    continue;

                foreach (var inv in memberDecl.DescendantNodes().OfType<InvocationExpressionSyntax>())
                {
                    var target = ResolveMethodSymbol(model.GetSymbolInfo(inv));
                    if (target == null) continue;
                    var targetKey = BuildMemberKey(target.OriginalDefinition);
                    if (targetKey == null) continue;
                    if (!memberLookup.TryGetValue(targetKey, out var targetNodeId)) continue;
                    if (targetNodeId == sourceNodeId) continue;

                    if (await TryCreateEdge(sourceNodeId, targetNodeId, "CALLS", edgeDedup))
                        callsCreated++;
                }

                foreach (var oc in memberDecl.DescendantNodes().OfType<ObjectCreationExpressionSyntax>())
                {
                    var target = ResolveMethodSymbol(model.GetSymbolInfo(oc));
                    if (target == null || target.MethodKind != MethodKind.Constructor) continue;
                    var targetKey = BuildMemberKey(target.OriginalDefinition);
                    if (targetKey == null) continue;
                    if (!memberLookup.TryGetValue(targetKey, out var targetNodeId)) continue;
                    if (targetNodeId == sourceNodeId) continue;

                    if (await TryCreateEdge(sourceNodeId, targetNodeId, "CALLS", edgeDedup))
                        callsCreated++;
                }

                foreach (var ma in memberDecl.DescendantNodes().OfType<MemberAccessExpressionSyntax>())
                {
                    // Skip method-name halves of an invocation (handled above).
                    if (ma.Parent is InvocationExpressionSyntax invParent && invParent.Expression == ma) continue;

                    var sym = ResolveFieldOrProperty(model.GetSymbolInfo(ma));
                    if (sym == null) continue;
                    var fieldKey = BuildFieldKey(sym);
                    if (fieldKey == null) continue;
                    if (!fieldLookup.TryGetValue(fieldKey, out var targetNodeId)) continue;
                    if (targetNodeId == sourceNodeId) continue;

                    if (await TryCreateEdge(sourceNodeId, targetNodeId, "REFERENCES", edgeDedup))
                        refsCreated++;
                }

                foreach (var id in memberDecl.DescendantNodes().OfType<IdentifierNameSyntax>())
                {
                    // Skip names already covered by MemberAccess/Invocation walks.
                    if (id.Parent is MemberAccessExpressionSyntax m && m.Name == id) continue;
                    if (id.Parent is InvocationExpressionSyntax inv2 && inv2.Expression == id) continue;

                    var sym = ResolveFieldOrProperty(model.GetSymbolInfo(id));
                    if (sym == null) continue;
                    var fieldKey = BuildFieldKey(sym);
                    if (fieldKey == null) continue;
                    if (!fieldLookup.TryGetValue(fieldKey, out var targetNodeId)) continue;
                    if (targetNodeId == sourceNodeId) continue;

                    if (await TryCreateEdge(sourceNodeId, targetNodeId, "REFERENCES", edgeDedup))
                        refsCreated++;
                }
            }
        }

        return new RebuildEdgesResult(filesProcessed, filesSkipped, callsCreated, refsCreated, "ok");
    }

    private async Task<bool> TryCreateEdge(Guid src, Guid dst, string label, HashSet<string> dedup)
    {
        var key = $"{label}|{src}|{dst}";
        if (!dedup.Add(key)) return false;
        try
        {
            await _age.CreateEdge(src, dst, label);
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[REBUILD]  edge {label} {src}->{dst} failed: {ex.Message}");
            return false;
        }
    }

    private static IMethodSymbol? ResolveMethodSymbol(SymbolInfo info)
    {
        if (info.Symbol is IMethodSymbol m) return m;
        return info.CandidateSymbols.OfType<IMethodSymbol>().FirstOrDefault();
    }

    private static ISymbol? ResolveFieldOrProperty(SymbolInfo info)
    {
        if (info.Symbol is IFieldSymbol or IPropertySymbol) return info.Symbol;
        foreach (var c in info.CandidateSymbols)
            if (c is IFieldSymbol or IPropertySymbol) return c;
        return null;
    }

    // ─── Lookup keys ─────────────────────────────────────────────────

    private static string? BuildMemberKey(IMethodSymbol method)
    {
        var typeKey = BuildTypeKey(method.ContainingType);
        if (typeKey == null) return null;
        var name = method.MethodKind == MethodKind.Constructor ? ".ctor" : method.Name;
        return $"{typeKey}::{name}/{method.Parameters.Length}";
    }

    private static string? BuildFieldKey(ISymbol sym)
    {
        if (sym.ContainingType == null) return null;
        var typeKey = BuildTypeKey(sym.ContainingType);
        if (typeKey == null) return null;
        return $"{typeKey}::{sym.Name}";
    }

    private static string? BuildTypeKey(INamedTypeSymbol? type)
    {
        if (type == null) return null;
        var typeChain = new List<string>();
        for (var cur = type; cur != null; cur = cur.ContainingType)
            typeChain.Add(cur.Name);
        typeChain.Reverse();

        var nsParts = new List<string>();
        for (var ns = type.ContainingNamespace; ns != null && !ns.IsGlobalNamespace; ns = ns.ContainingNamespace)
            nsParts.Add(ns.Name);
        nsParts.Reverse();

        var nsStr = string.Join(".", nsParts);
        var typeStr = string.Join(".", typeChain);
        return string.IsNullOrEmpty(nsStr) ? typeStr : $"{nsStr}.{typeStr}";
    }

    // ─── Index decomposed nodes into lookup tables ───────────────────

    private static void IndexFileNodes(
        List<TreeNode> nodes,
        Dictionary<string, Guid> memberLookup,
        Dictionary<string, Guid> fieldLookup)
    {
        if (nodes.Count == 0) return;

        var byParent = nodes.ToLookup(n => n.Record.ParentId);
        var nodeById = nodes.ToDictionary(n => n.Record.Id);

        foreach (var typeNode in nodes)
        {
            if (typeNode.Record.NodeType != "class" && typeNode.Record.NodeType != "struct")
                continue;

            var typeFullName = ComputeTypeFullName(typeNode, nodeById);
            if (string.IsNullOrEmpty(typeFullName)) continue;

            foreach (var child in byParent[typeNode.Record.Id])
            {
                switch (child.Record.NodeType)
                {
                    case "method":
                    {
                        if (child.Record.Name == null) continue;
                        var arity = byParent[child.Record.Id].Count(c => c.Record.NodeType == "parameter");
                        memberLookup[$"{typeFullName}::{child.Record.Name}/{arity}"] = child.Record.Id;
                        break;
                    }
                    case "constructor":
                    {
                        var arity = byParent[child.Record.Id].Count(c => c.Record.NodeType == "parameter");
                        memberLookup[$"{typeFullName}::.ctor/{arity}"] = child.Record.Id;
                        break;
                    }
                    case "field":
                    case "property":
                    {
                        if (child.Record.Name == null) continue;
                        fieldLookup[$"{typeFullName}::{child.Record.Name}"] = child.Record.Id;
                        break;
                    }
                }
            }
        }
    }

    /// <summary>
    /// Walk up the parent chain assembling "Namespace.Outer.Inner" for a type
    /// node. Handles nested types and nested namespace declarations.
    /// </summary>
    private static string ComputeTypeFullName(TreeNode typeNode, Dictionary<Guid, TreeNode> nodeById)
    {
        var typeChain = new List<string>();
        var nsParts = new List<string>();

        TreeNode? cur = typeNode;
        while (cur != null)
        {
            switch (cur.Record.NodeType)
            {
                case "class":
                case "struct":
                    if (!string.IsNullOrEmpty(cur.Record.Name))
                        typeChain.Add(cur.Record.Name);
                    break;
                case "namespace":
                    // Namespace name may itself be dotted ("Foo.Bar"); split so the
                    // sequence matches Roslyn's ContainingNamespace walk.
                    if (!string.IsNullOrEmpty(cur.Record.Name))
                        foreach (var part in cur.Record.Name.Split('.', StringSplitOptions.RemoveEmptyEntries))
                            nsParts.Add(part);
                    break;
            }
            cur = cur.Record.ParentId.HasValue && nodeById.TryGetValue(cur.Record.ParentId.Value, out var p) ? p : null;
        }

        // We added inner-to-outer; reverse so order is outer-to-inner.
        typeChain.Reverse();
        nsParts.Reverse();

        var nsStr = string.Join(".", nsParts);
        var typeStr = string.Join(".", typeChain);
        return string.IsNullOrEmpty(nsStr) ? typeStr : $"{nsStr}.{typeStr}";
    }

    // ─── File list query ─────────────────────────────────────────────

    private async Task<List<FileEntry>> ListProjectFiles(Guid projectId)
    {
        var files = new List<FileEntry>();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await using var cmd = new NpgsqlCommand(
            "SELECT id, file_path FROM files WHERE project_id = @p ORDER BY file_path", conn);
        cmd.Parameters.AddWithValue("p", projectId);
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
            files.Add(new FileEntry(reader.GetGuid(0), reader.GetString(1)));
        return files;
    }

    private record FileEntry(Guid Id, string Path);
}

public record RebuildEdgesResult(
    int FilesProcessed,
    int FilesSkipped,
    int CallsCreated,
    int ReferencesCreated,
    string Status);
