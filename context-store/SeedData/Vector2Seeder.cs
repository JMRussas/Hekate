// CodeStoragePoc - Vector2 Seed Data
//
// Populates the database with a Vector2 struct as structured nodes.
// Creates the full node tree: compilation_unit > namespace > struct > fields,
// constructor, methods, parameters, blocks, statements.
//
// Also creates AGE graph edges: CALLS, REFERENCES.
//
// SEED STRUCTURE (19 nodes):
//   compilation_unit
//   ├── using_directive "System"
//   └── namespace "CodeStoragePoc.Generated"
//       └── struct "Vector2"
//           ├── field "X" (float, public)
//           ├── field "Y" (float, public)
//           ├── constructor (public)
//           │   ├── parameter "x" (float)
//           │   ├── parameter "y" (float)
//           │   └── block
//           │       ├── statement "X = x;"
//           │       └── statement "Y = y;"
//           ├── method "Add" (public, returns Vector2)
//           │   ├── parameter "other" (Vector2)
//           │   └── block
//           │       └── statement "return new Vector2(X + other.X, Y + other.Y);"
//           └── method "ToString" (public override, returns string)
//               └── block
//                   └── statement "return $\"({X}, {Y})\";"
//
// Depends on: NodeRepository, AgeLayer
// Used by:    Program

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.GraphLayer;

namespace CodeStoragePoc.SeedData;

public class Vector2Seeder
{
    private readonly NodeRepository _repo;
    private Guid _projectId;
    private Guid _fileId;

    // Keep references to key nodes for edge creation
    private Guid _fieldX;
    private Guid _fieldY;
    private Guid _structNode;
    private Guid _ctorNode;
    private Guid _addNode;
    private Guid _toStringNode;

    public Vector2Seeder(NodeRepository repo) => _repo = repo;

    /// <summary>
    /// Seed the full Vector2 node tree. Returns the root node ID.
    /// </summary>
    public async Task<Guid> Seed(Guid projectId, Guid fileId)
    {
        _projectId = projectId;
        _fileId = fileId;

        // --- Root: compilation_unit ---
        var root = Guid.NewGuid();
        await Node(root, null, "compilation_unit", null, null, 0);

        // --- using System; ---
        var usingNode = Guid.NewGuid();
        await Node(usingNode, root, "using_directive", "System", null, 100);

        // --- namespace CodeStoragePoc.Generated ---
        var ns = Guid.NewGuid();
        await Node(ns, root, "namespace", "CodeStoragePoc.Generated", null, 200);

        // --- struct Vector2 ---
        _structNode = Guid.NewGuid();
        await Node(_structNode, ns, "struct", "Vector2", null, 100,
            new() { { "access", "public" } });

        // --- Fields ---
        _fieldX = Guid.NewGuid();
        await Node(_fieldX, _structNode, "field", "X", null, 100,
            new() { { "access", "public" }, { "type", "float" } });

        _fieldY = Guid.NewGuid();
        await Node(_fieldY, _structNode, "field", "Y", null, 200,
            new() { { "access", "public" }, { "type", "float" } });

        // --- Constructor ---
        _ctorNode = Guid.NewGuid();
        await Node(_ctorNode, _structNode, "constructor", null, null, 300,
            new() { { "access", "public" }, { "parent_type_name", "Vector2" } });

        var ctorParamX = Guid.NewGuid();
        await Node(ctorParamX, _ctorNode, "parameter", "x", null, 100,
            new() { { "type", "float" } });

        var ctorParamY = Guid.NewGuid();
        await Node(ctorParamY, _ctorNode, "parameter", "y", null, 200,
            new() { { "type", "float" } });

        var ctorBlock = Guid.NewGuid();
        await Node(ctorBlock, _ctorNode, "block", null, null, 300);

        await Node(Guid.NewGuid(), ctorBlock, "statement", null, "X = x;", 100);
        await Node(Guid.NewGuid(), ctorBlock, "statement", null, "Y = y;", 200);

        // --- Method: Add ---
        _addNode = Guid.NewGuid();
        await Node(_addNode, _structNode, "method", "Add", null, 400,
            new() { { "access", "public" }, { "return_type", "Vector2" } });

        var addParam = Guid.NewGuid();
        await Node(addParam, _addNode, "parameter", "other", null, 100,
            new() { { "type", "Vector2" } });

        var addBlock = Guid.NewGuid();
        await Node(addBlock, _addNode, "block", null, null, 200);

        await Node(Guid.NewGuid(), addBlock, "statement", null,
            "return new Vector2(X + other.X, Y + other.Y);", 100);

        // --- Method: ToString ---
        _toStringNode = Guid.NewGuid();
        await Node(_toStringNode, _structNode, "method", "ToString", null, 500,
            new() { { "access", "public" }, { "modifier", "override" }, { "return_type", "string" } });

        var toStrBlock = Guid.NewGuid();
        await Node(toStrBlock, _toStringNode, "block", null, null, 100);

        await Node(Guid.NewGuid(), toStrBlock, "statement", null,
            "return $\"({X}, {Y})\";", 100);

        _seeded = true;
        Console.WriteLine("[SEED]     Inserted 19 nodes for Vector2");
        return root;
    }

    /// <summary>
    /// Create AGE edges for the seed data.
    /// </summary>
    public async Task SeedEdges(AgeLayer age)
    {
        // Add CALLS: constructor creates a Vector2 instance (implied),
        // but more meaningfully, Add returns new Vector2(...)
        await age.CreateEdge(_addNode, _ctorNode, "CALLS");
        Console.WriteLine("[SEED]     Edge: Add -[CALLS]-> constructor");

        // Add REFERENCES: Add references X and Y
        await age.CreateEdge(_addNode, _fieldX, "REFERENCES");
        await age.CreateEdge(_addNode, _fieldY, "REFERENCES");
        Console.WriteLine("[SEED]     Edge: Add -[REFERENCES]-> X, Y");

        // ToString REFERENCES: X and Y
        await age.CreateEdge(_toStringNode, _fieldX, "REFERENCES");
        await age.CreateEdge(_toStringNode, _fieldY, "REFERENCES");
        Console.WriteLine("[SEED]     Edge: ToString -[REFERENCES]-> X, Y");

        Console.WriteLine("[SEED]     Edges created: CALLS(1) REFERENCES(4)");
    }

    /// <summary>
    /// Set a fake embedding on the Add method for the semantic search demo.
    /// </summary>
    public async Task SeedEmbeddings()
    {
        // Generate a normalized random vector for the Add method
        var rng = new Random(42);
        var embedding = new float[768];
        float norm = 0;
        for (int i = 0; i < embedding.Length; i++)
        {
            embedding[i] = (float)(rng.NextDouble() * 2 - 1);
            norm += embedding[i] * embedding[i];
        }
        norm = MathF.Sqrt(norm);
        for (int i = 0; i < embedding.Length; i++)
            embedding[i] /= norm;

        await _repo.SetEmbedding(_addNode, embedding);

        // Also set embeddings on ToString and constructor with slight noise
        var noise = 0.05f;
        var toStrEmbed = new float[768];
        var ctorEmbed = new float[768];
        for (int i = 0; i < 768; i++)
        {
            toStrEmbed[i] = embedding[i] + (float)(rng.NextDouble() - 0.5) * noise;
            ctorEmbed[i] = embedding[i] + (float)(rng.NextDouble() - 0.5) * noise * 2;
        }
        await _repo.SetEmbedding(_toStringNode, Normalize(toStrEmbed));
        await _repo.SetEmbedding(_ctorNode, Normalize(ctorEmbed));

        Console.WriteLine("[SEED]     Embeddings set on Add, ToString, constructor");
    }

    /// <summary>
    /// Get the Add method's embedding for the search demo.
    /// Returns the same seeded embedding with slight noise added.
    /// </summary>
    public float[] GetQueryVector()
    {
        var rng = new Random(42);
        var vec = new float[768];
        float norm = 0;
        for (int i = 0; i < vec.Length; i++)
        {
            vec[i] = (float)(rng.NextDouble() * 2 - 1);
            norm += vec[i] * vec[i];
        }
        norm = MathF.Sqrt(norm);
        for (int i = 0; i < vec.Length; i++)
            vec[i] /= norm;

        // Add slight noise to simulate a different but similar query
        var noise = 0.02f;
        var rng2 = new Random(99);
        for (int i = 0; i < vec.Length; i++)
            vec[i] += (float)(rng2.NextDouble() - 0.5) * noise;

        return Normalize(vec);
    }

    // Key node IDs for the mutation demo (only valid after Seed() has been called)
    private bool _seeded;
    public Guid AddMethodId => _seeded ? _addNode : throw new InvalidOperationException("Seed() must be called first");
    public Guid FieldXId => _seeded ? _fieldX : throw new InvalidOperationException("Seed() must be called first");
    public Guid FieldYId => _seeded ? _fieldY : throw new InvalidOperationException("Seed() must be called first");
    public Guid StructId => _seeded ? _structNode : throw new InvalidOperationException("Seed() must be called first");

    private async Task Node(Guid id, Guid? parent, string type, string? name,
        string? value, int order, Dictionary<string, string>? attrs = null)
    {
        await _repo.InsertNode(id, _projectId, _fileId, type, name, value,
            parent, order, "seeder", attrs);
    }

    private static float[] Normalize(float[] vec)
    {
        float norm = 0;
        for (int i = 0; i < vec.Length; i++)
            norm += vec[i] * vec[i];
        norm = MathF.Sqrt(norm);
        if (norm > 0)
            for (int i = 0; i < vec.Length; i++)
                vec[i] /= norm;
        return vec;
    }
}
