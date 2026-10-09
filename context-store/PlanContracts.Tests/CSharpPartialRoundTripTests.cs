using CodeStoragePoc.DbLayer;
using CodeStoragePoc.Decomposer;
using CodeStoragePoc.Generator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Xunit;

namespace CodeStoragePoc.PlanContracts.Tests;

/// <summary>
/// C# partial round-trip: real CSharpDecomposer + CSharpGenerator over an in-memory
/// ICodeNodeRepository, with every generated file compiled by Roslyn. See
/// Decomposer/PARTIAL-CONTRACT.md. No database or services.
/// </summary>
public class CSharpPartialRoundTripTests
{
    // --- fake store ---------------------------------------------------------

    /// <summary>In-memory repository mirroring NodeRepository semantics: fresh trees per read,
    /// files unique per (project, path), node deletion scoped to a file.</summary>
    private sealed class FakeRepo : ICodeNodeRepository
    {
        private readonly Dictionary<(Guid Project, string Path), Guid> _files = new();
        private readonly Dictionary<Guid, Guid> _fileRoots = new();
        private readonly Dictionary<Guid, (NodeRecord Record, Dictionary<string, string> Attrs)> _nodes = new();

        public int Count => _nodes.Count;
        public Guid? RootOf(Guid fileId) => _fileRoots.TryGetValue(fileId, out var r) ? r : null;

        public Task<Guid> GetOrCreateFile(Guid projectId, string filePath)
        {
            if (!_files.TryGetValue((projectId, filePath), out var id))
                _files[(projectId, filePath)] = id = Guid.NewGuid();
            return Task.FromResult(id);
        }

        public Task DeleteNodesByFile(Guid fileId)
        {
            foreach (var id in _nodes.Where(n => n.Value.Record.FileId == fileId).Select(n => n.Key).ToList())
                _nodes.Remove(id);
            return Task.CompletedTask;
        }

        public Task<Guid> InsertNode(Guid id, Guid projectId, Guid? fileId, string nodeType, string? name,
            string? value, Guid? parentId, int siblingOrder, string? modifiedBy = null,
            Dictionary<string, string>? attributes = null)
        {
            var now = DateTime.UtcNow;
            var record = new NodeRecord(id, projectId, fileId, nodeType, name, value, parentId, siblingOrder, now, now, modifiedBy);
            _nodes.Add(id, (record, attributes == null ? new() : new Dictionary<string, string>(attributes)));
            return Task.FromResult(id);
        }

        public Task UpdateFileRootNode(Guid fileId, Guid rootNodeId)
        {
            _fileRoots[fileId] = rootNodeId;
            return Task.CompletedTask;
        }

        public Task<TreeNode?> GetSubtree(Guid rootId)
        {
            if (!_nodes.ContainsKey(rootId))
                return Task.FromResult<TreeNode?>(null);

            var built = new Dictionary<Guid, TreeNode>();
            var queue = new Queue<Guid>();
            queue.Enqueue(rootId);
            while (queue.Count > 0)
            {
                var id = queue.Dequeue();
                built[id] = Fresh(id);
                foreach (var child in _nodes.Values.Where(n => n.Record.ParentId == id))
                    queue.Enqueue(child.Record.Id);
            }
            foreach (var node in built.Values)
            {
                if (node.Record.Id != rootId && built.TryGetValue(node.Record.ParentId!.Value, out var parent))
                    parent.Children.Add(node);
            }
            foreach (var node in built.Values)
                node.Children.Sort((a, b) => a.Record.SiblingOrder.CompareTo(b.Record.SiblingOrder));
            return Task.FromResult<TreeNode?>(built[rootId]);
        }

        public Task<List<TreeNode>> GetNodesByFile(Guid fileId)
        {
            var list = _nodes.Values
                .Where(n => n.Record.FileId == fileId)
                .OrderBy(n => n.Record.ParentId.HasValue ? 1 : 0)
                .ThenBy(n => n.Record.ParentId)
                .ThenBy(n => n.Record.SiblingOrder)
                .Select(n => Fresh(n.Record.Id))
                .ToList();
            return Task.FromResult(list);
        }

        private TreeNode Fresh(Guid id)
        {
            var (record, attrs) = _nodes[id];
            return new TreeNode(record) { Attributes = new Dictionary<string, string>(attrs) };
        }
    }

    private static readonly Guid Project = Guid.Parse("11111111-1111-1111-1111-111111111111");

    private static (FakeRepo Repo, CSharpDecomposer Decomposer, CSharpGenerator Generator) Create()
    {
        var repo = new FakeRepo();
        return (repo, new CSharpDecomposer(repo), new CSharpGenerator(repo));
    }

    // --- compiler -----------------------------------------------------------

    private static readonly Lazy<IReadOnlyList<MetadataReference>> References = new(() =>
    {
        var tpa = (string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!;
        return tpa.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
            .Select(p => (MetadataReference)MetadataReference.CreateFromFile(p))
            .ToList();
    });

    private static void AssertCompiles(params string[] sources)
    {
        var options = new CSharpParseOptions(LanguageVersion.Latest);
        var trees = sources.Select(s => CSharpSyntaxTree.ParseText(s, options)).ToList();
        var compilation = CSharpCompilation.Create("PartialRoundTrip_" + Guid.NewGuid().ToString("N"), trees, References.Value,
            new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
        var errors = compilation.GetDiagnostics()
            .Where(d => d.Severity == DiagnosticSeverity.Error)
            .Select(d => d.ToString())
            .ToList();
        Assert.True(errors.Count == 0,
            string.Join(Environment.NewLine, errors) + Environment.NewLine + "---" + Environment.NewLine +
            string.Join(Environment.NewLine + "---" + Environment.NewLine, sources));
    }

    private static async Task<string> RoundTrip(FakeRepo repo, CSharpDecomposer d, CSharpGenerator g, string path, string source)
    {
        var root = await d.DecomposeSourceAsync(Project, path, source);
        var generated = await g.Generate(root);
        var fileId = await repo.GetOrCreateFile(Project, path);
        Assert.Equal(generated, await g.GenerateFromFile(fileId));
        return generated;
    }

    // --- sources ------------------------------------------------------------

    private const string WidgetDeclarations = """
        using System;

        namespace Demo;

        public partial class Widget
        {
            public int Value;

            partial void OnChanged(int v);
            partial void Log(string s);
            public static partial int Compute(int x);
            public partial string Describe();
            internal partial bool Check(int x);

            public void Touch()
            {
                OnChanged(Value);
                Log("touch");
            }
        }
        """;

    private const string WidgetImplementations = """
        using System;

        namespace Demo;

        public partial class Widget
        {
            partial void OnChanged(int v)
            {
                Console.WriteLine(v);
            }

            partial void Log(string s) => Console.WriteLine(s);

            public static partial int Compute(int x) { return x * 2; }

            public partial string Describe() => "w" + Value;

            internal partial bool Check(int x) => x > Value;
        }
        """;

    // --- tests --------------------------------------------------------------

    [Fact]
    public async Task Decomposer_persists_is_partial_on_types_and_methods_only()
    {
        var (repo, d, g) = Create();
        await d.DecomposeSourceAsync(Project, "Widget.cs", WidgetDeclarations);
        var nodes = await repo.GetNodesByFile(await repo.GetOrCreateFile(Project, "Widget.cs"));

        Assert.Equal("true", nodes.Single(n => n.Record.NodeType == "class").Attr("is_partial"));
        var partials = nodes.Where(n => n.Record.NodeType == "method" && n.Attr("is_partial") == "true")
            .Select(n => n.Record.Name).OrderBy(x => x).ToList();
        Assert.Equal(new[] { "Check", "Compute", "Describe", "Log", "OnChanged" }, partials);
        Assert.Equal("", nodes.Single(n => n.Record.Name == "Touch").Attr("is_partial"));

        var onChanged = nodes.Single(n => n.Record.Name == "OnChanged");
        Assert.Equal("implicit", onChanged.Attr("access"));   // stored metadata, never a keyword
        Assert.DoesNotContain(nodes, n => n.Record.NodeType == "block" && n.Record.ParentId == onChanged.Record.Id);   // declaration-only: no body
    }

    [Fact]
    public async Task Partial_class_pieces_round_trip_and_compile_together()
    {
        var (repo, d, g) = Create();
        var decl = await RoundTrip(repo, d, g, "Widget.Decl.cs", WidgetDeclarations);
        var impl = await RoundTrip(repo, d, g, "Widget.Impl.cs", WidgetImplementations);

        Assert.Contains("public partial class Widget", decl);
        Assert.Contains("public partial class Widget", impl);
        Assert.Contains("partial void OnChanged(int v);", decl);
        Assert.Contains("public static partial int Compute(int x);", decl);
        Assert.Contains("public partial string Describe();", decl);
        Assert.Contains("internal partial bool Check(int x);", decl);
        Assert.Contains("partial void OnChanged(int v)", impl);
        Assert.DoesNotContain("implicit", decl + impl);

        AssertCompiles(decl, impl);
    }

    [Fact]
    public async Task Expression_bodied_partial_void_does_not_emit_return()
    {
        var (repo, d, g) = Create();
        var impl = await RoundTrip(repo, d, g, "Widget.Impl.cs", WidgetImplementations);

        Assert.Contains("Console.WriteLine(s);", impl);
        Assert.DoesNotContain("return Console.WriteLine", impl);
        Assert.Contains("return \"w\" + Value;", impl);   // non-void expression bodies still return
        AssertCompiles(await RoundTrip(repo, d, g, "Widget.Decl.cs", WidgetDeclarations), impl);
    }

    [Fact]
    public async Task Partial_struct_pieces_round_trip_and_compile_together()
    {
        const string a = """
            namespace Demo
            {
                public partial struct Point
                {
                    public int X;
                    public partial int Sum();
                    partial void Reset();
                }
            }
            """;
        const string b = """
            namespace Demo
            {
                public partial struct Point
                {
                    public int Y;
                    public partial int Sum() => X + Y;
                    partial void Reset() { }
                }
            }
            """;
        var (repo, d, g) = Create();
        var ga = await RoundTrip(repo, d, g, "Point.A.cs", a);
        var gb = await RoundTrip(repo, d, g, "Point.B.cs", b);

        Assert.Contains("public partial struct Point", ga);
        Assert.Contains("public partial struct Point", gb);
        AssertCompiles(ga, gb);
    }

    [Fact]
    public async Task Static_partial_class_and_implicit_access_type_compile()
    {
        const string a = """
            namespace Demo;

            public static partial class Util
            {
                public static partial int Twice(int x);
            }

            partial class Hidden
            {
                partial void Hook();
            }
            """;
        const string b = """
            namespace Demo;

            public static partial class Util
            {
                public static partial int Twice(int x) => x * 2;
            }

            partial class Hidden
            {
                partial void Hook() { }
            }
            """;
        var (repo, d, g) = Create();
        var ga = await RoundTrip(repo, d, g, "Util.A.cs", a);
        var gb = await RoundTrip(repo, d, g, "Util.B.cs", b);

        Assert.Contains("public static partial class Util", ga);
        Assert.Contains("partial class Hidden", ga);
        Assert.DoesNotContain("implicit", ga + gb);
        AssertCompiles(ga, gb);
    }

    [Fact]
    public async Task DecomposeFileAsync_from_temp_file_round_trips()
    {
        var path = Path.Combine(Path.GetTempPath(), $"partial-{Guid.NewGuid():N}.cs");
        await File.WriteAllTextAsync(path, WidgetDeclarations);
        try
        {
            var (repo, d, g) = Create();
            var root = await d.DecomposeFileAsync(Project, path);
            var fileId = await repo.GetOrCreateFile(Project, path);
            Assert.Equal(root, repo.RootOf(fileId));

            var generated = await g.GenerateFromFile(fileId);
            Assert.Equal(await g.Generate(root), generated);
            Assert.Contains("partial void OnChanged(int v);", generated);

            var impl = await RoundTrip(repo, d, g, "Widget.Impl.cs", WidgetImplementations);
            AssertCompiles(generated, impl);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public async Task Same_file_redecompose_replaces_nodes_and_keeps_other_files()
    {
        var (repo, d, g) = Create();
        await RoundTrip(repo, d, g, "Widget.Decl.cs", WidgetDeclarations);
        var otherBefore = await RoundTrip(repo, d, g, "Widget.Impl.cs", WidgetImplementations);
        var otherFile = await repo.GetOrCreateFile(Project, "Widget.Impl.cs");
        var otherNodeCount = (await repo.GetNodesByFile(otherFile)).Count;
        var total = repo.Count;

        var first = await RoundTrip(repo, d, g, "Widget.Decl.cs", WidgetDeclarations);
        Assert.Equal(total, repo.Count);   // old nodes replaced, not accumulated

        const string changed = """
            namespace Demo;

            public partial class Widget
            {
                partial void OnChanged(int v);
            }
            """;
        var second = await RoundTrip(repo, d, g, "Widget.Decl.cs", changed);
        Assert.NotEqual(first, second);
        Assert.DoesNotContain("Compute", second);
        Assert.Contains("partial void OnChanged(int v);", second);

        Assert.Equal(otherNodeCount, (await repo.GetNodesByFile(otherFile)).Count);
        Assert.Equal(otherBefore, await g.GenerateFromFile(otherFile));
    }

    [Fact]
    public async Task Files_with_same_path_in_different_projects_stay_separate()
    {
        var (repo, d, g) = Create();
        var otherProject = Guid.NewGuid();
        var rootA = await d.DecomposeSourceAsync(Project, "Widget.cs", WidgetDeclarations);
        var rootB = await d.DecomposeSourceAsync(otherProject, "Widget.cs", WidgetImplementations);

        var fileA = await repo.GetOrCreateFile(Project, "Widget.cs");
        var fileB = await repo.GetOrCreateFile(otherProject, "Widget.cs");
        Assert.NotEqual(fileA, fileB);
        Assert.NotEqual(rootA, rootB);
        Assert.Contains("partial void OnChanged(int v);", await g.GenerateFromFile(fileA));
        Assert.DoesNotContain("partial void OnChanged(int v);", await g.GenerateFromFile(fileB));
    }

    [Fact]
    public async Task Fake_repository_returns_fresh_trees_per_read()
    {
        var (repo, d, g) = Create();
        var root = await d.DecomposeSourceAsync(Project, "Widget.cs", WidgetDeclarations);

        var t1 = await repo.GetSubtree(root);
        var t2 = await repo.GetSubtree(root);
        Assert.NotSame(t1, t2);
        t1!.Children.Clear();
        t1.Attributes["x"] = "y";
        Assert.NotEmpty(t2!.Children);
        Assert.NotEmpty((await repo.GetSubtree(root))!.Children);

        // Generating twice is stable even though generate mutates its own fetched tree.
        var fileId = await repo.GetOrCreateFile(Project, "Widget.cs");
        Assert.Equal(await g.GenerateFromFile(fileId), await g.GenerateFromFile(fileId));
    }

    [Fact]
    public async Task Nonpartial_control_is_unchanged_and_compiles()
    {
        const string source = """
            using System;

            namespace Demo;

            public class Plain
            {
                private int _n;

                public Plain(int n)
                {
                    _n = n;
                }

                public int Get() { return _n; }

                public static int Twice(int x) => x * 2;

                void Hidden() { }

                public void Show() => Console.WriteLine(_n);
            }
            """;
        var (repo, d, g) = Create();
        var generated = await RoundTrip(repo, d, g, "Plain.cs", source);

        Assert.DoesNotContain("partial", generated);
        Assert.DoesNotContain("implicit", generated);
        Assert.Contains("public class Plain", generated);
        Assert.Contains("public int Get()", generated);
        Assert.Contains("public static int Twice(int x)", generated);
        Assert.Contains("    void Hidden()", generated);
        Assert.DoesNotContain("Get();", generated);   // methods with bodies get no declaration semicolon
        Assert.DoesNotContain("return Console.WriteLine", generated);

        var nodes = await repo.GetNodesByFile(await repo.GetOrCreateFile(Project, "Plain.cs"));
        Assert.DoesNotContain(nodes, n => n.Attributes.ContainsKey("is_partial"));
        AssertCompiles(generated);
    }
}
