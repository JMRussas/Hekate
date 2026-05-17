// CodeStoragePoc - C# Code Generator
//
// Walks a node tree (fetched from the DB) and emits valid C# source code.
// This is the core of the DB-backed code storage approach: the node tree
// IS the source of truth, and this generator produces compilable files.
//
// TRAVERSAL STRATEGY:
// 1. Fetch the entire subtree from the root node in one recursive CTE query
// 2. Build an in-memory tree (TreeNode with children)
// 3. Walk depth-first, emitting C# syntax based on node_type
// 4. Each node type has a handler that:
//    - Reads the node's name, value, and attributes
//    - Emits appropriate C# keywords and punctuation
//    - Recursively processes children in sibling_order
//
// NODE TYPES SUPPORTED:
//   compilation_unit  — file root, emits children sequentially
//   using_directive   — emits "using {Name};"
//   namespace         — emits "namespace {Name};" (file-scoped)
//   struct / class    — emits type declaration with braces
//   field             — emits "{access} {type} {Name};"
//   constructor       — emits "{access} {ParentName}({params}) { body }"
//   method            — emits "{modifiers} {return_type} {Name}({params}) { body }"
//   parameter         — formatted as "{type} {Name}" in parameter lists
//   block             — emits "{ ... }" with indented children
//   statement         — emits the Value directly (leaf node)
//
// Depends on: NodeRepository (for tree fetching)
// Used by:    Program

using System.Text;
using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.Generator;

public class CSharpGenerator
{
    private readonly NodeRepository _repo;

    public CSharpGenerator(NodeRepository repo) => _repo = repo;

    /// <summary>
    /// Generate C# source code from a node tree rooted at the given ID.
    /// </summary>
    public async Task<string> Generate(Guid rootNodeId)
    {
        var tree = await _repo.GetSubtree(rootNodeId);
        if (tree == null)
            throw new InvalidOperationException($"No node tree found for root {rootNodeId}");

        var sb = new StringBuilder();
        EmitNode(tree, sb, indent: 0);
        return sb.ToString();
    }

    /// <summary>
    /// Generate C# source from all nodes belonging to a file.
    /// Reconstructs the tree from flat nodes and emits source.
    /// </summary>
    public async Task<string> GenerateFromFile(Guid fileId)
    {
        var nodes = await _repo.GetNodesByFile(fileId);
        if (nodes.Count == 0)
            throw new InvalidOperationException($"No nodes found for file {fileId}");

        // Find the compilation_unit root
        var root = nodes.FirstOrDefault(n => n.Record.NodeType == "compilation_unit")
            ?? nodes.First(n => n.Record.ParentId == null);

        // Build tree from flat list
        var lookup = nodes.ToDictionary(n => n.Record.Id);
        foreach (var node in nodes)
        {
            if (node.Record.ParentId.HasValue && lookup.TryGetValue(node.Record.ParentId.Value, out var parent))
                parent.Children.Add(node);
        }
        foreach (var node in nodes)
            node.Children.Sort((a, b) => a.Record.SiblingOrder.CompareTo(b.Record.SiblingOrder));

        var sb = new StringBuilder();
        EmitNode(root, sb, indent: 0);
        return sb.ToString();
    }

    /// <summary>
    /// Recursive node emitter. Dispatches on node_type to produce C# syntax.
    /// Each case is responsible for its own formatting and child traversal.
    /// </summary>
    private void EmitNode(TreeNode node, StringBuilder sb, int indent)
    {
        switch (node.Record.NodeType)
        {
            // --- FILE ROOT ---
            // The compilation_unit is the top-level container.
            // It emits all children (using directives, namespace, types) sequentially.
            case "compilation_unit":
                foreach (var child in node.Children)
                    EmitNode(child, sb, indent);
                break;

            // --- USING DIRECTIVE ---
            // Emits: using System;
            case "using_directive":
                sb.AppendLine($"{Indent(indent)}using {node.Record.Name};");
                break;

            // --- NAMESPACE ---
            // Uses file-scoped namespace (C# 10+): namespace Foo.Bar;
            // Emits a blank line before and after the declaration.
            case "namespace":
                sb.AppendLine();
                sb.AppendLine($"{Indent(indent)}namespace {node.Record.Name};");
                sb.AppendLine();
                foreach (var child in node.Children)
                    EmitNode(child, sb, indent);
                break;

            // --- STRUCT / CLASS ---
            // Emits the type declaration with access modifier and braces.
            // Children are fields, constructors, methods.
            case "struct":
            case "class":
            {
                var access = node.Attr("access", "public");
                var isPartialType = node.Attr("is_partial", "") == "true";
                var keyword = node.Record.NodeType;
                var partialKw = isPartialType ? "partial " : "";
                sb.AppendLine($"{Indent(indent)}{access} {partialKw}{keyword} {node.Record.Name}");
                sb.AppendLine($"{Indent(indent)}{{");
                foreach (var child in node.Children)
                    EmitNode(child, sb, indent + 1);
                sb.AppendLine($"{Indent(indent)}}}");
                break;
            }

            // --- FIELD ---
            // Emits: public float X;
            // Reads access and type from attributes.
            case "field":
            {
                var access = node.Attr("access", "public");
                var type = node.Attr("type", "object");
                sb.AppendLine($"{Indent(indent)}{access} {type} {node.Record.Name};");
                break;
            }

            // --- CONSTRUCTOR ---
            // Emits: public TypeName(params) { body }
            // The parent type name is looked up from the tree structure.
            // Parameters are child nodes of type "parameter".
            // The body is a child node of type "block".
            case "constructor":
            {
                var access = node.Attr("access", "public");
                // Find the parent type name — we walk up to find a struct/class ancestor.
                // Since we have the tree in memory, we get the parent name from attributes.
                var typeName = node.Attr("parent_type_name", "Unknown");
                var parameters = node.Children.Where(c => c.Record.NodeType == "parameter").ToList();
                var paramStr = FormatParameters(parameters);

                sb.AppendLine();
                sb.AppendLine($"{Indent(indent)}{access} {typeName}({paramStr})");

                // Emit the block
                var block = node.Children.FirstOrDefault(c => c.Record.NodeType == "block");
                if (block != null)
                    EmitNode(block, sb, indent);
                break;
            }

            // --- METHOD ---
            // Emits: public [modifiers] ReturnType Name(params) { body }
            // Modifiers like "override" come from the "modifier" attribute.
            case "method":
            {
                var access = node.Attr("access", "public");
                var modifier = node.Attr("modifier", "");
                var isPartial = node.Attr("is_partial", "") == "true";
                var returnType = node.Attr("return_type", "void");
                var parameters = node.Children.Where(c => c.Record.NodeType == "parameter").ToList();
                var paramStr = FormatParameters(parameters);

                // Build the declaration line
                var parts = new List<string> { access };
                if (isPartial) parts.Add("partial");
                if (!string.IsNullOrEmpty(modifier))
                    parts.Add(modifier);
                parts.Add(returnType);
                parts.Add($"{node.Record.Name}({paramStr})");

                sb.AppendLine();
                var block = node.Children.FirstOrDefault(c => c.Record.NodeType == "block");
                if (block != null)
                {
                    sb.AppendLine($"{Indent(indent)}{string.Join(" ", parts)}");
                    EmitNode(block, sb, indent);
                }
                else if (isPartial)
                {
                    // Declaration-only partial method — no body, terminated with semicolon
                    sb.AppendLine($"{Indent(indent)}{string.Join(" ", parts)};");
                }
                else
                {
                    sb.AppendLine($"{Indent(indent)}{string.Join(" ", parts)}");
                }
                break;
            }

            // --- BLOCK ---
            // Emits: { ... } with children indented one level.
            // Children are typically statement nodes.
            case "block":
                sb.AppendLine($"{Indent(indent)}{{");
                foreach (var child in node.Children)
                    EmitNode(child, sb, indent + 1);
                sb.AppendLine($"{Indent(indent)}}}");
                break;

            // --- STATEMENT ---
            // Leaf node. The Value field contains the complete C# statement text.
            // This is the pragmatic granularity choice: instead of decomposing every
            // expression into sub-nodes, we store complete statements as text.
            // This gives us node-level mutability while keeping the tree manageable.
            case "statement":
                sb.AppendLine($"{Indent(indent)}{node.Record.Value}");
                break;

            // --- PROPERTY ---
            // Emits the full property text stored in Value (round-trip from decomposer).
            case "property":
                sb.AppendLine($"{Indent(indent)}{node.Record.Value}");
                break;

            // --- ENUM ---
            // Emits the full enum text stored in Value (round-trip from decomposer).
            case "enum":
                sb.AppendLine();
                sb.AppendLine($"{Indent(indent)}{node.Record.Value}");
                break;

            // --- PARAMETER ---
            // Not emitted directly — used by constructor/method for parameter lists.
            case "parameter":
                break;

            default:
                // Unknown node type — emit as a comment to make it visible
                sb.AppendLine($"{Indent(indent)}// [unknown node_type: {node.Record.NodeType}] {node.Record.Name}");
                break;
        }
    }

    /// <summary>
    /// Format a list of parameter nodes as a C# parameter list string.
    /// Each parameter has a "type" attribute and a Name.
    /// Example output: "float x, float y"
    /// </summary>
    private static string FormatParameters(List<TreeNode> parameters)
    {
        return string.Join(", ", parameters.Select(p =>
        {
            var type = p.Attr("type", "object");
            return $"{type} {p.Record.Name}";
        }));
    }

    private static string Indent(int level) => new(' ', level * 4);
}
