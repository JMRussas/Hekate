// CodeStoragePoc - Roslyn Decomposer
//
// Parses a C# source file using the Roslyn compiler and decomposes it into
// a tree of nodes in the database. This is the inverse of CSharpGenerator:
//   CSharpGenerator: DB nodes → C# source
//   RoslynDecomposer: C# source → DB nodes
//
// Together they prove the round-trip: file → parse → DB → generate → compile → execute.
//
// STRATEGY:
// Walk the Roslyn SyntaxTree depth-first. For each recognized syntax node,
// create a DB node with the appropriate type, name, value, and attributes.
// Unrecognized constructs are stored as statement-level text nodes (pragmatic
// granularity — we don't need to decompose every expression).
//
// Depends on: Microsoft.CodeAnalysis.CSharp, NodeRepository
// Used by:    Program (git→DB→execute demo)

using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.Generator;

public class RoslynDecomposer
{
    private readonly NodeRepository _repo;
    private readonly List<Guid> _allNodeIds = new();

    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public RoslynDecomposer(NodeRepository repo) => _repo = repo;

    /// <summary>
    /// Parse a C# source string and insert all nodes into the database.
    /// Returns the root (compilation_unit) node ID.
    /// </summary>
    public async Task<Guid> Decompose(string source, Guid projectId, Guid fileId, string? agent = null)
    {
        var tree = CSharpSyntaxTree.ParseText(source);
        var root = tree.GetCompilationUnitRoot();

        var rootId = Guid.NewGuid();
        await _repo.InsertNode(rootId, projectId, fileId,
            "compilation_unit", null, null, null, 0, agent);
        _allNodeIds.Add(rootId);

        int order = 100;
        foreach (var member in root.Members)
        {
            await DecompileMember(member, rootId, projectId, fileId, order, agent);
            order += 100;
        }

        // Using directives first
        int usingOrder = 10;
        foreach (var u in root.Usings)
        {
            var uId = Guid.NewGuid();
            await _repo.InsertNode(uId, projectId, fileId,
                "using_directive", u.Name?.ToString(), null, rootId, usingOrder, agent);
            _allNodeIds.Add(uId);
            usingOrder += 10;
        }

        return rootId;
    }

    private async Task DecompileMember(MemberDeclarationSyntax member,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        switch (member)
        {
            case FileScopedNamespaceDeclarationSyntax ns:
                await DecomposeNamespace(ns, parentId, projectId, fileId, order, agent);
                break;
            case NamespaceDeclarationSyntax ns:
                await DecomposeNamespace(ns.Name.ToString(), ns.Members, parentId, projectId, fileId, order, agent);
                break;
            case ClassDeclarationSyntax cls:
                await DecomposeType(cls, "class", parentId, projectId, fileId, order, agent);
                break;
            case StructDeclarationSyntax str:
                await DecomposeType(str, "struct", parentId, projectId, fileId, order, agent);
                break;
            default:
                // Store unrecognized members as statement text
                var sId = Guid.NewGuid();
                await _repo.InsertNode(sId, projectId, fileId,
                    "statement", null, member.ToFullString().Trim(), parentId, order, agent);
                _allNodeIds.Add(sId);
                break;
        }
    }

    private async Task DecomposeNamespace(FileScopedNamespaceDeclarationSyntax ns,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var nsId = Guid.NewGuid();
        await _repo.InsertNode(nsId, projectId, fileId,
            "namespace", ns.Name.ToString(), null, parentId, order, agent);
        _allNodeIds.Add(nsId);

        int childOrder = 100;
        foreach (var m in ns.Members)
        {
            await DecompileMember(m, nsId, projectId, fileId, childOrder, agent);
            childOrder += 100;
        }
    }

    private async Task DecomposeNamespace(string name, SyntaxList<MemberDeclarationSyntax> members,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var nsId = Guid.NewGuid();
        await _repo.InsertNode(nsId, projectId, fileId,
            "namespace", name, null, parentId, order, agent);
        _allNodeIds.Add(nsId);

        int childOrder = 100;
        foreach (var m in members)
        {
            await DecompileMember(m, nsId, projectId, fileId, childOrder, agent);
            childOrder += 100;
        }
    }

    private async Task DecomposeType(TypeDeclarationSyntax type, string nodeType,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var typeId = Guid.NewGuid();
        var modifiers = type.Modifiers.ToString();
        var access = modifiers.Contains("public") ? "public" :
                     modifiers.Contains("internal") ? "internal" : "internal";

        var attrs = new Dictionary<string, string> { { "access", access } };
        if (modifiers.Contains("static")) attrs["modifier"] = "static";

        await _repo.InsertNode(typeId, projectId, fileId,
            nodeType, type.Identifier.Text, null, parentId, order, agent, attrs);
        _allNodeIds.Add(typeId);

        int childOrder = 100;
        foreach (var m in type.Members)
        {
            switch (m)
            {
                case FieldDeclarationSyntax field:
                    await DecomposeField(field, typeId, projectId, fileId, childOrder, agent);
                    break;
                case ConstructorDeclarationSyntax ctor:
                    await DecomposeConstructor(ctor, type.Identifier.Text, typeId, projectId, fileId, childOrder, agent);
                    break;
                case MethodDeclarationSyntax method:
                    await DecomposeMethod(method, typeId, projectId, fileId, childOrder, agent);
                    break;
                default:
                    var sId = Guid.NewGuid();
                    await _repo.InsertNode(sId, projectId, fileId,
                        "statement", null, m.ToFullString().Trim(), typeId, childOrder, agent);
                    _allNodeIds.Add(sId);
                    break;
            }
            childOrder += 100;
        }
    }

    private async Task DecomposeField(FieldDeclarationSyntax field,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var modifiers = field.Modifiers.ToString();
        var access = modifiers.Contains("public") ? "public" :
                     modifiers.Contains("private") ? "private" : "private";
        var type = field.Declaration.Type.ToString();

        foreach (var variable in field.Declaration.Variables)
        {
            var fId = Guid.NewGuid();
            var attrs = new Dictionary<string, string>
            {
                { "access", access },
                { "type", type },
            };
            await _repo.InsertNode(fId, projectId, fileId,
                "field", variable.Identifier.Text, null, parentId, order, agent, attrs);
            _allNodeIds.Add(fId);
            order += 10;
        }
    }

    private async Task DecomposeConstructor(ConstructorDeclarationSyntax ctor, string typeName,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var ctorId = Guid.NewGuid();
        var access = ctor.Modifiers.ToString().Contains("public") ? "public" : "private";
        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "parent_type_name", typeName },
        };

        await _repo.InsertNode(ctorId, projectId, fileId,
            "constructor", null, null, parentId, order, agent, attrs);
        _allNodeIds.Add(ctorId);

        // Parameters
        int paramOrder = 100;
        foreach (var p in ctor.ParameterList.Parameters)
        {
            var pId = Guid.NewGuid();
            await _repo.InsertNode(pId, projectId, fileId,
                "parameter", p.Identifier.Text, null, ctorId, paramOrder, agent,
                new() { { "type", p.Type?.ToString() ?? "object" } });
            _allNodeIds.Add(pId);
            paramOrder += 100;
        }

        // Body block
        if (ctor.Body != null)
            await DecomposeBlock(ctor.Body, ctorId, projectId, fileId, paramOrder + 100, agent);
    }

    private async Task DecomposeMethod(MethodDeclarationSyntax method,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var methodId = Guid.NewGuid();
        var modifiers = method.Modifiers.ToString();
        var access = modifiers.Contains("public") ? "public" :
                     modifiers.Contains("private") ? "private" : "private";

        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "return_type", method.ReturnType.ToString() },
        };

        // Collect non-access modifiers (static, override, virtual, etc.)
        var otherMods = method.Modifiers
            .Where(m => m.Text != "public" && m.Text != "private" && m.Text != "protected" && m.Text != "internal")
            .Select(m => m.Text);
        var modifier = string.Join(" ", otherMods);
        if (!string.IsNullOrEmpty(modifier))
            attrs["modifier"] = modifier;

        await _repo.InsertNode(methodId, projectId, fileId,
            "method", method.Identifier.Text, null, parentId, order, agent, attrs);
        _allNodeIds.Add(methodId);

        // Parameters
        int paramOrder = 100;
        foreach (var p in method.ParameterList.Parameters)
        {
            var pId = Guid.NewGuid();
            await _repo.InsertNode(pId, projectId, fileId,
                "parameter", p.Identifier.Text, null, methodId, paramOrder, agent,
                new() { { "type", p.Type?.ToString() ?? "object" } });
            _allNodeIds.Add(pId);
            paramOrder += 100;
        }

        // Body block
        if (method.Body != null)
            await DecomposeBlock(method.Body, methodId, projectId, fileId, paramOrder + 100, agent);
    }

    private async Task DecomposeBlock(BlockSyntax block,
        Guid parentId, Guid projectId, Guid fileId, int order, string? agent)
    {
        var blockId = Guid.NewGuid();
        await _repo.InsertNode(blockId, projectId, fileId,
            "block", null, null, parentId, order, agent);
        _allNodeIds.Add(blockId);

        int stmtOrder = 100;
        foreach (var stmt in block.Statements)
        {
            var sId = Guid.NewGuid();
            // Store each statement as leaf text — pragmatic granularity
            await _repo.InsertNode(sId, projectId, fileId,
                "statement", null, stmt.ToFullString().Trim(), blockId, stmtOrder, agent);
            _allNodeIds.Add(sId);
            stmtOrder += 100;
        }
    }
}
