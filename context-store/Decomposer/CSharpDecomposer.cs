// CodeStoragePoc - C# Decomposer
//
// Parses C# source files using Roslyn and decomposes them into structured
// nodes matching CodeStoragePoc's deep node model (parameter/block/statement
// children). Ported from db-ast-poc's RoslynDecomposer, adapted for the
// node_attributes table and sibling_order column.
//
// The decomposer is idempotent: re-decomposing a file deletes old nodes
// and inserts fresh ones. Node IDs change on re-decompose (acceptable for POC).
//
// Node structure matches Vector2Seeder pattern:
//   compilation_unit > namespace > struct/class > field/constructor/method
//   Methods have parameter children + block > statement children.
//
// Depends on: DbLayer/NodeRepository, Microsoft.CodeAnalysis.CSharp
// Used by:    Api/Services/CodeService

using CodeStoragePoc.DbLayer;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CodeStoragePoc.Decomposer;

public class CSharpDecomposer
{
    private readonly NodeRepository _repo;

    public CSharpDecomposer(NodeRepository repo) => _repo = repo;

    /// <summary>
    /// Decompose a C# source file into nodes and persist to the database.
    /// Returns the root node ID (compilation_unit).
    /// </summary>
    public async Task<Guid> DecomposeFileAsync(Guid projectId, string filePath)
    {
        var sourceText = await File.ReadAllTextAsync(filePath);
        return await DecomposeSourceAsync(projectId, filePath, sourceText);
    }

    /// <summary>
    /// Decompose C# source text into nodes and persist to the database.
    /// Returns the root node ID (compilation_unit).
    /// </summary>
    public async Task<Guid> DecomposeSourceAsync(Guid projectId, string filePath, string sourceText)
    {
        var fileId = await _repo.GetOrCreateFile(projectId, filePath);

        // Delete existing nodes for this file (clean re-decompose)
        await _repo.DeleteNodesByFile(fileId);

        // Parse and insert
        var rootId = await ParseAndInsert(projectId, fileId, sourceText);

        // Update file's root_node_id
        await _repo.UpdateFileRootNode(fileId, rootId);

        return rootId;
    }

    /// <summary>
    /// Pure parse + insert: walks Roslyn tree and creates nodes in the DB.
    /// Returns the root compilation_unit node ID.
    /// </summary>
    private async Task<Guid> ParseAndInsert(Guid projectId, Guid fileId, string sourceText)
    {
        var tree = CSharpSyntaxTree.ParseText(sourceText,
            new CSharpParseOptions(LanguageVersion.Latest));
        var root = tree.GetCompilationUnitRoot();

        // Root: compilation_unit
        var rootId = Guid.NewGuid();
        await _repo.InsertNode(rootId, projectId, fileId, "compilation_unit",
            null, null, null, 0, "decomposer");

        int order = 100;

        // Using directives (top-level)
        foreach (var u in root.Usings)
        {
            var name = u.Name?.ToString() ?? u.ToString().Trim();
            await _repo.InsertNode(Guid.NewGuid(), projectId, fileId,
                "using_directive", name, null, rootId, order, "decomposer");
            order += 100;
        }

        // Members
        foreach (var member in root.Members)
        {
            await WalkMember(member, projectId, fileId, rootId, order);
            order += 100;
        }

        return rootId;
    }

    /// <summary>
    /// Recursively walks a member declaration and inserts nodes.
    /// </summary>
    private async Task WalkMember(MemberDeclarationSyntax member, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        switch (member)
        {
            case FileScopedNamespaceDeclarationSyntax fileScopedNs:
            {
                var nsId = Guid.NewGuid();
                await _repo.InsertNode(nsId, projectId, fileId, "namespace",
                    fileScopedNs.Name.ToString(), null, parentId, siblingOrder, "decomposer");

                int order = 100;
                foreach (var u in fileScopedNs.Usings)
                {
                    var name = u.Name?.ToString() ?? u.ToString().Trim();
                    await _repo.InsertNode(Guid.NewGuid(), projectId, fileId,
                        "using_directive", name, null, nsId, order, "decomposer");
                    order += 100;
                }
                foreach (var m in fileScopedNs.Members)
                {
                    await WalkMember(m, projectId, fileId, nsId, order);
                    order += 100;
                }
                break;
            }

            case NamespaceDeclarationSyntax ns:
            {
                var nsId = Guid.NewGuid();
                await _repo.InsertNode(nsId, projectId, fileId, "namespace",
                    ns.Name.ToString(), null, parentId, siblingOrder, "decomposer");

                int order = 100;
                foreach (var u in ns.Usings)
                {
                    var name = u.Name?.ToString() ?? u.ToString().Trim();
                    await _repo.InsertNode(Guid.NewGuid(), projectId, fileId,
                        "using_directive", name, null, nsId, order, "decomposer");
                    order += 100;
                }
                foreach (var m in ns.Members)
                {
                    await WalkMember(m, projectId, fileId, nsId, order);
                    order += 100;
                }
                break;
            }

            case ClassDeclarationSyntax classDecl:
                await InsertTypeNode("class", classDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case StructDeclarationSyntax structDecl:
                await InsertTypeNode("struct", structDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case EnumDeclarationSyntax enumDecl:
                await InsertEnumNode(enumDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case MethodDeclarationSyntax methodDecl:
                await InsertMethodNode(methodDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case ConstructorDeclarationSyntax ctorDecl:
                await InsertConstructorNode(ctorDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case FieldDeclarationSyntax fieldDecl:
                await InsertFieldNode(fieldDecl, projectId, fileId, parentId, siblingOrder);
                break;

            case PropertyDeclarationSyntax propDecl:
                await InsertPropertyNode(propDecl, projectId, fileId, parentId, siblingOrder);
                break;
        }
    }

    // --- Node insertion helpers ---

    private async Task InsertTypeNode(string nodeType, TypeDeclarationSyntax typeDecl,
        Guid projectId, Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = typeDecl.Modifiers.Select(m => m.Text).ToList();
        var access = ExtractAccess(modifiers);

        var typeId = Guid.NewGuid();
        var attrs = new Dictionary<string, string> { { "access", access } };

        if (modifiers.Contains("static"))
            attrs["is_static"] = "true";

        var baseTypes = typeDecl.BaseList?.Types.Select(t => t.ToString()).ToList();
        if (baseTypes is { Count: > 0 })
            attrs["base_types"] = string.Join(", ", baseTypes);

        var genericParams = typeDecl.TypeParameterList?.Parameters
            .Select(p => p.Identifier.Text).ToList();
        if (genericParams is { Count: > 0 })
            attrs["generic_params"] = string.Join(", ", genericParams);

        // Build signature for round-trip: "public struct Vector2"
        var sig = $"{string.Join(" ", modifiers)} {nodeType} {typeDecl.Identifier.Text}{typeDecl.TypeParameterList}".Trim();
        attrs["signature"] = sig;

        await _repo.InsertNode(typeId, projectId, fileId, nodeType,
            typeDecl.Identifier.Text, null, parentId, siblingOrder, "decomposer", attrs);

        // Recurse into members
        int order = 100;
        foreach (var m in typeDecl.Members)
        {
            await WalkMember(m, projectId, fileId, typeId, order);
            order += 100;
        }
    }

    private async Task InsertMethodNode(MethodDeclarationSyntax m, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = m.Modifiers.Select(mod => mod.Text).ToList();
        var access = ExtractAccess(modifiers);
        var returnType = m.ReturnType.ToString();

        var methodId = Guid.NewGuid();
        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "return_type", returnType },
        };

        if (modifiers.Contains("override"))
            attrs["modifier"] = "override";
        else if (modifiers.Contains("virtual"))
            attrs["modifier"] = "virtual";
        else if (modifiers.Contains("abstract"))
            attrs["modifier"] = "abstract";

        if (modifiers.Contains("static"))
            attrs["is_static"] = "true";

        await _repo.InsertNode(methodId, projectId, fileId, "method",
            m.Identifier.Text, null, parentId, siblingOrder, "decomposer", attrs);

        // Parameters as children
        int order = 100;
        foreach (var param in m.ParameterList.Parameters)
        {
            var paramAttrs = new Dictionary<string, string>
            {
                { "type", param.Type?.ToString() ?? "object" }
            };
            await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "parameter",
                param.Identifier.Text, null, methodId, order, "decomposer", paramAttrs);
            order += 100;
        }

        // Block with statement children
        if (m.Body != null)
        {
            await InsertBlock(m.Body, projectId, fileId, methodId, order);
        }
        else if (m.ExpressionBody != null)
        {
            // Expression-bodied: create block with single return statement
            var blockId = Guid.NewGuid();
            await _repo.InsertNode(blockId, projectId, fileId, "block",
                null, null, methodId, order, "decomposer");
            await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "statement",
                null, $"return {m.ExpressionBody.Expression};", blockId, 100, "decomposer");
        }
    }

    private async Task InsertConstructorNode(ConstructorDeclarationSyntax c, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = c.Modifiers.Select(mod => mod.Text).ToList();
        var access = ExtractAccess(modifiers);

        var ctorId = Guid.NewGuid();
        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "parent_type_name", c.Identifier.Text },
        };

        await _repo.InsertNode(ctorId, projectId, fileId, "constructor",
            null, null, parentId, siblingOrder, "decomposer", attrs);

        // Parameters
        int order = 100;
        foreach (var param in c.ParameterList.Parameters)
        {
            var paramAttrs = new Dictionary<string, string>
            {
                { "type", param.Type?.ToString() ?? "object" }
            };
            await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "parameter",
                param.Identifier.Text, null, ctorId, order, "decomposer", paramAttrs);
            order += 100;
        }

        // Block
        if (c.Body != null)
        {
            await InsertBlock(c.Body, projectId, fileId, ctorId, order);
        }
    }

    private async Task InsertFieldNode(FieldDeclarationSyntax f, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = f.Modifiers.Select(mod => mod.Text).ToList();
        var access = ExtractAccess(modifiers);
        var firstVar = f.Declaration.Variables.First();
        var fieldType = f.Declaration.Type.ToString();

        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "type", fieldType },
        };

        if (modifiers.Contains("static"))
            attrs["is_static"] = "true";
        if (modifiers.Contains("readonly"))
            attrs["modifier"] = "readonly";

        await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "field",
            firstVar.Identifier.Text, null, parentId, siblingOrder, "decomposer", attrs);
    }

    private async Task InsertPropertyNode(PropertyDeclarationSyntax p, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = p.Modifiers.Select(mod => mod.Text).ToList();
        var access = ExtractAccess(modifiers);
        var propType = p.Type.ToString();

        var attrs = new Dictionary<string, string>
        {
            { "access", access },
            { "type", propType },
        };

        if (modifiers.Contains("static"))
            attrs["is_static"] = "true";

        // Store the full property text as value for round-trip fidelity
        await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "property",
            p.Identifier.Text, p.ToString().Trim(), parentId, siblingOrder, "decomposer", attrs);
    }

    private async Task InsertEnumNode(EnumDeclarationSyntax e, Guid projectId,
        Guid fileId, Guid parentId, int siblingOrder)
    {
        var modifiers = e.Modifiers.Select(mod => mod.Text).ToList();
        var access = ExtractAccess(modifiers);

        var attrs = new Dictionary<string, string> { { "access", access } };

        // Store full enum text as value for round-trip
        await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "enum",
            e.Identifier.Text, e.ToString().Trim(), parentId, siblingOrder, "decomposer", attrs);
    }

    /// <summary>Insert a block node with statement children for each statement.</summary>
    private async Task InsertBlock(BlockSyntax block, Guid projectId, Guid fileId,
        Guid parentId, int siblingOrder)
    {
        var blockId = Guid.NewGuid();
        await _repo.InsertNode(blockId, projectId, fileId, "block",
            null, null, parentId, siblingOrder, "decomposer");

        int order = 100;
        foreach (var stmt in block.Statements)
        {
            await _repo.InsertNode(Guid.NewGuid(), projectId, fileId, "statement",
                null, stmt.ToString().Trim(), blockId, order, "decomposer");
            order += 100;
        }
    }

    /// <summary>Extract access modifier from a list of modifiers. Returns "implicit" if none declared.</summary>
    private static string ExtractAccess(List<string> modifiers)
    {
        var accessModifiers = new[] { "public", "private", "protected", "internal" };
        return modifiers.FirstOrDefault(m => accessModifiers.Contains(m)) ?? "implicit";
    }
}
