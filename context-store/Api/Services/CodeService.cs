// CodeStoragePoc.Api - Code Service
//
// Orchestrates code decomposition (file → nodes) and materialization
// (nodes → source). Combines CSharpDecomposer, CSharpGenerator,
// EdgeSeeder, and AgeLayer into a single service for API endpoints.
//
// Depends on: Decomposer/CSharpDecomposer, Generator/CSharpGenerator,
//             Decomposer/EdgeSeeder, GraphLayer/AgeLayer, DbLayer/NodeRepository
// Used by:    Api/Program.cs endpoints

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.Decomposer;
using CodeStoragePoc.Generator;
using CodeStoragePoc.GraphLayer;

namespace CodeStoragePoc.Api.Services;

public class CodeService
{
    private readonly NodeRepository _repo;
    private readonly AgeLayer _age;
    private readonly string _connStr;

    public CodeService(NodeRepository repo, AgeLayer age, string connStr)
    {
        _repo = repo;
        _age = age;
        _connStr = connStr;
    }

    /// <summary>
    /// Decompose a C# file from disk into nodes. Returns summary info.
    /// </summary>
    public async Task<DecomposeResult> DecomposeFile(Guid projectId, string filePath)
    {
        if (!File.Exists(filePath))
            throw new FileNotFoundException($"File not found: {filePath}");

        var decomposer = new CSharpDecomposer(_repo);
        var rootId = await decomposer.DecomposeFileAsync(projectId, filePath);
        return await FinalizeDecomposition(projectId, filePath, rootId);
    }

    /// <summary>
    /// Decompose C# source text (posted in request body) into nodes.
    /// </summary>
    public async Task<DecomposeResult> DecomposeSource(Guid projectId, string filePath, string sourceText)
    {
        var decomposer = new CSharpDecomposer(_repo);
        var rootId = await decomposer.DecomposeSourceAsync(projectId, filePath, sourceText);
        return await FinalizeDecomposition(projectId, filePath, rootId);
    }

    private async Task<DecomposeResult> FinalizeDecomposition(Guid projectId, string filePath, Guid rootId)
    {
        try
        {
            await _age.SyncAllVertices(_repo, projectId);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] AGE sync failed: {ex.Message}");
        }

        var fileId = await _repo.GetOrCreateFile(projectId, filePath);
        int calls = 0, refs = 0;
        try
        {
            var seeder = new EdgeSeeder(_repo, _age);
            (calls, refs) = await seeder.SeedEdgesForFile(fileId);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Edge seeding failed: {ex.Message}");
        }

        var nodes = await _repo.GetNodesByFile(fileId);
        return new DecomposeResult(rootId, fileId, filePath, nodes.Count, calls, refs);
    }

    /// <summary>
    /// Materialize (generate) C# source from a file's node tree.
    /// </summary>
    public async Task<string> MaterializeFile(Guid fileId)
    {
        var generator = new CSharpGenerator(_repo);
        return await generator.GenerateFromFile(fileId);
    }

    /// <summary>
    /// Materialize from a root node ID.
    /// </summary>
    public async Task<string> MaterializeNode(Guid rootNodeId)
    {
        var generator = new CSharpGenerator(_repo);
        return await generator.Generate(rootNodeId);
    }

    /// <summary>
    /// Rebuild semantic edges (CALLS / REFERENCES) for an entire project using
    /// Roslyn symbol resolution. Bypasses the body-text EdgeSeeder heuristic
    /// so calls and field/property accesses can cross file boundaries.
    /// </summary>
    public async Task<RebuildEdgesResult> RebuildSemanticEdges(Guid projectId)
    {
        var resolver = new SemanticEdgeResolver(_repo, _age, _connStr);
        return await resolver.RebuildForProject(projectId);
    }

    /// <summary>
    /// List all files tracked in a project.
    /// </summary>
    public async Task<List<FileInfo>> ListFiles(Guid projectId)
    {
        await using var conn = new Npgsql.NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        var files = new List<FileInfo>();
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT f.id, f.file_path, f.root_node_id,
                   (SELECT COUNT(*) FROM nodes WHERE file_id = f.id) as node_count
            FROM files f WHERE f.project_id = @proj
            ORDER BY f.file_path";
        cmd.Parameters.AddWithValue("proj", projectId);

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            files.Add(new FileInfo(
                reader.GetGuid(0),
                reader.GetString(1),
                reader.IsDBNull(2) ? null : reader.GetGuid(2),
                reader.GetInt64(3)
            ));
        }

        return files;
    }
}

public record DecomposeResult(Guid RootNodeId, Guid FileId, string FilePath, int NodeCount, int Calls, int References);
public record FileInfo(Guid Id, string FilePath, Guid? RootNodeId, long NodeCount);
