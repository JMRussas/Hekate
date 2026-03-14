// CodeStoragePoc - Node Repository
//
// CRUD operations for the nodes and node_attributes tables.
// All queries are raw SQL via Npgsql — no ORM.
// Provides subtree fetching via recursive CTE for the code generator.
//
// CONNECTION PATTERN:
// Constructor accepts an optional NpgsqlConnection + NpgsqlTransaction.
// - If provided: uses that connection for all queries (lock-safe mutations).
// - If not: opens a new connection per operation (read-only convenience).
// This lets SubtreeLock.LockConnection flow through to mutations.
//
// Depends on: Npgsql
// Used by:    CSharpGenerator, ContextAssembler, PlanRenderer, ConversationRenderer,
//             Vector2Seeder, PlanSeeder, Plan002Seeder, ConversationSeeder, Program

using Npgsql;

namespace CodeStoragePoc.DbLayer;

/// <summary>Flat record matching the nodes table.</summary>
public record NodeRecord(
    Guid Id,
    Guid ProjectId,
    Guid? FileId,
    string NodeType,
    string? Name,
    string? Value,
    Guid? ParentId,
    int SiblingOrder,
    DateTime CreatedAt,
    DateTime ModifiedAt,
    string? ModifiedBy);

/// <summary>A node with its attributes and children, for in-memory tree traversal.</summary>
public class TreeNode
{
    public NodeRecord Record { get; }
    public Dictionary<string, string> Attributes { get; set; } = new();
    public List<TreeNode> Children { get; } = new();

    public TreeNode(NodeRecord record) => Record = record;

    public string Attr(string key, string fallback = "") =>
        Attributes.TryGetValue(key, out var v) ? v : fallback;
}

public class NodeRepository
{
    private readonly string _connStr;
    private readonly NpgsqlConnection? _externalConn;
    private readonly NpgsqlTransaction? _externalTx;

    /// <summary>
    /// Create a repository that opens its own connections per operation.
    /// Use for read-only operations or when no lock is held.
    /// </summary>
    public NodeRepository(string connectionString)
    {
        _connStr = connectionString;
    }

    /// <summary>
    /// Create a repository that uses an existing connection (and optional transaction).
    /// Use when mutations must happen on the lock connection.
    /// </summary>
    public NodeRepository(NpgsqlConnection connection, NpgsqlTransaction? transaction = null)
    {
        _connStr = connection.ConnectionString;
        _externalConn = connection;
        _externalTx = transaction;
    }

    /// <summary>
    /// Get a connection for the current operation.
    /// If an external connection was provided, returns it (caller must not dispose).
    /// Otherwise, opens a new connection (caller must dispose).
    /// </summary>
    private async Task<(NpgsqlConnection conn, bool owned)> GetConnection()
    {
        if (_externalConn != null)
            return (_externalConn, false);

        var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        return (conn, true);
    }

    /// <summary>Insert a node with optional attributes. Returns the node ID.</summary>
    public async Task<Guid> InsertNode(
        Guid id, Guid projectId, Guid? fileId,
        string nodeType, string? name, string? value,
        Guid? parentId, int siblingOrder,
        string? modifiedBy = null,
        Dictionary<string, string>? attributes = null)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            // Wrap insert + attributes in a transaction for atomicity
            // (only if we don't already have an external transaction)
            NpgsqlTransaction? tx = _externalTx;
            bool ownTx = false;
            if (tx == null)
            {
                tx = await conn.BeginTransactionAsync();
                ownTx = true;
            }

            try
            {
                await using var cmd = new NpgsqlCommand("""
                    INSERT INTO nodes (id, project_id, file_id, node_type, name, value,
                                       parent_id, sibling_order, modified_by)
                    VALUES (@id, @proj, @file, @type, @name, @val, @parent, @order, @agent)
                """, conn, tx);
                cmd.Parameters.AddWithValue("id", id);
                cmd.Parameters.AddWithValue("proj", projectId);
                cmd.Parameters.AddWithValue("file", (object?)fileId ?? DBNull.Value);
                cmd.Parameters.AddWithValue("type", nodeType);
                cmd.Parameters.AddWithValue("name", (object?)name ?? DBNull.Value);
                cmd.Parameters.AddWithValue("val", (object?)value ?? DBNull.Value);
                cmd.Parameters.AddWithValue("parent", (object?)parentId ?? DBNull.Value);
                cmd.Parameters.AddWithValue("order", siblingOrder);
                cmd.Parameters.AddWithValue("agent", (object?)modifiedBy ?? DBNull.Value);
                await cmd.ExecuteNonQueryAsync();

                // Insert attributes within the same transaction
                if (attributes != null)
                {
                    foreach (var (key, val) in attributes)
                        await InsertAttribute(conn, tx, id, key, val);
                }

                if (ownTx)
                    await tx.CommitAsync();
            }
            catch
            {
                if (ownTx)
                    await tx.RollbackAsync();
                throw;
            }
            finally
            {
                if (ownTx)
                    await tx.DisposeAsync();
            }

            return id;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Set or update a single attribute on a node.</summary>
    public async Task SetAttribute(Guid nodeId, string key, string value)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await InsertAttribute(conn, _externalTx, nodeId, key, value);
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Delete a single attribute from a node.</summary>
    public async Task DeleteAttribute(Guid nodeId, string key)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "DELETE FROM node_attributes WHERE node_id = @node AND key = @key", conn, _externalTx);
            cmd.Parameters.AddWithValue("node", nodeId);
            cmd.Parameters.AddWithValue("key", key);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    private static async Task InsertAttribute(NpgsqlConnection conn, NpgsqlTransaction? tx, Guid nodeId, string key, string value)
    {
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO node_attributes (id, node_id, key, value)
            VALUES (gen_random_uuid(), @node, @key, @val)
            ON CONFLICT (node_id, key) DO UPDATE SET value = @val
        """, conn, tx);
        cmd.Parameters.AddWithValue("node", nodeId);
        cmd.Parameters.AddWithValue("key", key);
        cmd.Parameters.AddWithValue("val", value);
        await cmd.ExecuteNonQueryAsync();
    }

    /// <summary>Fetch a single node by ID.</summary>
    public async Task<NodeRecord?> GetNode(Guid id)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT * FROM nodes WHERE id = @id", conn);
            cmd.Parameters.AddWithValue("id", id);

            await using var reader = await cmd.ExecuteReaderAsync();
            return await reader.ReadAsync() ? ReadNode(reader) : null;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>
    /// Fetch an entire subtree rooted at the given node ID.
    /// Returns a TreeNode with children populated recursively, plus all attributes loaded.
    /// Uses a recursive CTE to fetch all descendants in one query.
    /// </summary>
    public async Task<TreeNode?> GetSubtree(Guid rootId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            // Fetch all nodes in the subtree
            var nodes = new Dictionary<Guid, TreeNode>();
            await using (var cmd = new NpgsqlCommand("""
                WITH RECURSIVE subtree AS (
                    SELECT * FROM nodes WHERE id = @root
                    UNION ALL
                    SELECT n.* FROM nodes n JOIN subtree s ON n.parent_id = s.id
                )
                SELECT * FROM subtree ORDER BY parent_id NULLS FIRST, sibling_order
            """, conn))
            {
                cmd.Parameters.AddWithValue("root", rootId);
                await using var reader = await cmd.ExecuteReaderAsync();
                while (await reader.ReadAsync())
                {
                    var record = ReadNode(reader);
                    nodes[record.Id] = new TreeNode(record);
                }
            }

            if (nodes.Count == 0) return null;

            // Fetch all attributes for these nodes in one query
            var nodeIds = nodes.Keys.ToArray();
            await using (var cmd = new NpgsqlCommand("""
                SELECT node_id, key, value FROM node_attributes
                WHERE node_id = ANY(@ids)
            """, conn))
            {
                cmd.Parameters.AddWithValue("ids", nodeIds);
                await using var reader = await cmd.ExecuteReaderAsync();
                while (await reader.ReadAsync())
                {
                    var nodeId = reader.GetGuid(0);
                    var key = reader.GetString(1);
                    var val = reader.GetString(2);
                    if (nodes.TryGetValue(nodeId, out var node))
                        node.Attributes[key] = val;
                }
            }

            // Build the tree: wire children to parents
            TreeNode? root = null;
            foreach (var node in nodes.Values)
            {
                if (node.Record.ParentId.HasValue &&
                    nodes.TryGetValue(node.Record.ParentId.Value, out var parent))
                {
                    parent.Children.Add(node);
                }
                else if (node.Record.Id == rootId)
                {
                    root = node;
                }
            }

            // Sort children by sibling_order
            foreach (var node in nodes.Values)
                node.Children.Sort((a, b) => a.Record.SiblingOrder.CompareTo(b.Record.SiblingOrder));

            return root;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Update a node's value and/or name. Bumps modified_at.</summary>
    public async Task UpdateNode(Guid id, string? name = null, string? value = null, string? modifiedBy = null)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand("""
                UPDATE nodes SET
                    name = COALESCE(@name, name),
                    value = COALESCE(@val, value),
                    modified_by = COALESCE(@agent, modified_by),
                    modified_at = now()
                WHERE id = @id
            """, conn, _externalTx);
            cmd.Parameters.AddWithValue("id", id);
            cmd.Parameters.AddWithValue("name", (object?)name ?? DBNull.Value);
            cmd.Parameters.AddWithValue("val", (object?)value ?? DBNull.Value);
            cmd.Parameters.AddWithValue("agent", (object?)modifiedBy ?? DBNull.Value);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>
    /// Updates a node with explicit flags to distinguish "not provided" from "set to null".
    /// Runs within the caller's transaction for advisory lock safety.
    /// Uses dynamic SET clauses — only touches columns explicitly flagged for update.
    /// </summary>
    public static async Task MutateAsync(Guid id,
        string? name, bool updateName,
        string? value, bool updateValue,
        string agentId, NpgsqlConnection conn, NpgsqlTransaction tx)
    {
        var setClauses = new List<string> { "modified_at = now()", "modified_by = @agent" };
        if (updateName) setClauses.Add("name = @name");
        if (updateValue) setClauses.Add("value = @val");

        var sql = $"UPDATE nodes SET {string.Join(", ", setClauses)} WHERE id = @id;";
        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        cmd.Parameters.AddWithValue("id", id);
        cmd.Parameters.AddWithValue("agent", agentId);
        if (updateName) cmd.Parameters.AddWithValue("name", (object?)name ?? DBNull.Value);
        if (updateValue) cmd.Parameters.AddWithValue("val", (object?)value ?? DBNull.Value);
        await cmd.ExecuteNonQueryAsync();
    }

    /// <summary>Delete a node and all its descendants (CASCADE handles attributes).</summary>
    public async Task DeleteSubtree(Guid rootId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand("""
                WITH RECURSIVE subtree AS (
                    SELECT id FROM nodes WHERE id = @root
                    UNION ALL
                    SELECT n.id FROM nodes n JOIN subtree s ON n.parent_id = s.id
                )
                DELETE FROM nodes WHERE id IN (SELECT id FROM subtree)
            """, conn, _externalTx);
            cmd.Parameters.AddWithValue("root", rootId);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Get the next available sibling_order for children of a parent.</summary>
    public async Task<int> NextSiblingOrder(Guid parentId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            // Lock the parent row to prevent concurrent sibling_order races
            await using (var lockCmd = new NpgsqlCommand(
                "SELECT id FROM nodes WHERE id = @parent FOR UPDATE", conn, _externalTx))
            {
                lockCmd.Parameters.AddWithValue("parent", parentId);
                await lockCmd.ExecuteNonQueryAsync();
            }

            await using var cmd = new NpgsqlCommand("""
                SELECT COALESCE(MAX(sibling_order), 0) + 100 FROM nodes WHERE parent_id = @parent
            """, conn);
            cmd.Parameters.AddWithValue("parent", parentId);
            var result = await cmd.ExecuteScalarAsync();
            return Convert.ToInt32(result);
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Set the embedding vector on a node (768-dim float array as text).</summary>
    public async Task SetEmbedding(Guid nodeId, float[] vector)
    {
        if (vector.Length != 768)
            throw new ArgumentException($"Embedding vector must be 768 dimensions, got {vector.Length}.");
        if (vector.Any(v => float.IsNaN(v) || float.IsInfinity(v)))
            throw new ArgumentException("Embedding vector contains NaN or Infinity values.");

        var (conn, owned) = await GetConnection();
        try
        {
            // pgvector accepts vectors as '[1,2,3,...]' text format
            var vectorStr = "[" + string.Join(",", vector.Select(v => v.ToString("G"))) + "]";

            await using var cmd = new NpgsqlCommand(
                "UPDATE nodes SET embedding = @vec::vector WHERE id = @id", conn, _externalTx);
            cmd.Parameters.AddWithValue("id", nodeId);
            cmd.Parameters.AddWithValue("vec", vectorStr);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Find the top N most similar nodes by cosine distance.</summary>
    public async Task<List<(Guid Id, string? Name, string NodeType, double Distance)>>
        SemanticSearch(float[] queryVector, int topN = 3)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            var vecStr = "[" + string.Join(",", queryVector.Select(v => v.ToString("G"))) + "]";

            await using var cmd = new NpgsqlCommand($"""
                SELECT id, name, node_type, embedding <=> @vec::vector AS distance
                FROM nodes
                WHERE embedding IS NOT NULL
                ORDER BY distance
                LIMIT @n
            """, conn);
            cmd.Parameters.AddWithValue("vec", vecStr);
            cmd.Parameters.AddWithValue("n", topN);

            var results = new List<(Guid, string?, string, double)>();
            await using var reader = await cmd.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                results.Add((
                    reader.GetGuid(0),
                    reader.IsDBNull(1) ? null : reader.GetString(1),
                    reader.GetString(2),
                    reader.GetDouble(3)
                ));
            }
            return results;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Get the parent node of a given node.</summary>
    public async Task<TreeNode?> GetParent(Guid nodeId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            // Get parent_id, then fetch that node with its attributes
            await using var cmd = new NpgsqlCommand(
                "SELECT parent_id FROM nodes WHERE id = @id", conn);
            cmd.Parameters.AddWithValue("id", nodeId);
            var parentId = await cmd.ExecuteScalarAsync();

            if (parentId == null || parentId is DBNull) return null;
            return await GetNodeWithAttributes((Guid)parentId);
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Get direct children of a node (one level, not recursive), with attributes.</summary>
    public async Task<List<TreeNode>> GetChildren(Guid parentId, string? filterType = null)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            var sql = filterType != null
                ? "SELECT * FROM nodes WHERE parent_id = @parent AND node_type = @type ORDER BY sibling_order"
                : "SELECT * FROM nodes WHERE parent_id = @parent ORDER BY sibling_order";

            await using var cmd = new NpgsqlCommand(sql, conn);
            cmd.Parameters.AddWithValue("parent", parentId);
            if (filterType != null)
                cmd.Parameters.AddWithValue("type", filterType);

            var nodes = new List<TreeNode>();
            await using (var reader = await cmd.ExecuteReaderAsync())
            {
                while (await reader.ReadAsync())
                    nodes.Add(new TreeNode(ReadNode(reader)));
            }

            // Batch-load attributes
            if (nodes.Count > 0)
                await LoadAttributes(conn, nodes);

            return nodes;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Get a single node with its attributes loaded.</summary>
    public async Task<TreeNode?> GetNodeWithAttributes(Guid id)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT * FROM nodes WHERE id = @id", conn);
            cmd.Parameters.AddWithValue("id", id);

            await using var reader = await cmd.ExecuteReaderAsync();
            if (!await reader.ReadAsync()) return null;

            var node = new TreeNode(ReadNode(reader));
            await reader.CloseAsync();

            // Load attributes
            await using var attrCmd = new NpgsqlCommand(
                "SELECT key, value FROM node_attributes WHERE node_id = @id", conn);
            attrCmd.Parameters.AddWithValue("id", id);
            await using var attrReader = await attrCmd.ExecuteReaderAsync();
            while (await attrReader.ReadAsync())
                node.Attributes[attrReader.GetString(0)] = attrReader.GetString(1);

            return node;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Get siblings of a node (other children of the same parent), with attributes.</summary>
    public async Task<List<TreeNode>> GetSiblings(Guid nodeId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand("""
                SELECT * FROM nodes
                WHERE parent_id = (SELECT parent_id FROM nodes WHERE id = @id)
                AND id != @id
                ORDER BY sibling_order
            """, conn);
            cmd.Parameters.AddWithValue("id", nodeId);

            var nodes = new List<TreeNode>();
            await using (var reader = await cmd.ExecuteReaderAsync())
            {
                while (await reader.ReadAsync())
                    nodes.Add(new TreeNode(ReadNode(reader)));
            }

            if (nodes.Count > 0)
                await LoadAttributes(conn, nodes);

            return nodes;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    /// <summary>Batch-load attributes for a list of nodes.</summary>
    private static async Task LoadAttributes(NpgsqlConnection conn, List<TreeNode> nodes)
    {
        var ids = nodes.Select(n => n.Record.Id).ToArray();
        await using var cmd = new NpgsqlCommand("""
            SELECT node_id, key, value FROM node_attributes
            WHERE node_id = ANY(@ids)
        """, conn);
        cmd.Parameters.AddWithValue("ids", ids);

        var lookup = nodes.ToDictionary(n => n.Record.Id);
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var nodeId = reader.GetGuid(0);
            if (lookup.TryGetValue(nodeId, out var node))
                node.Attributes[reader.GetString(1)] = reader.GetString(2);
        }
    }

    /// <summary>Get all node IDs in the project (for AGE sync).</summary>
    public async Task<List<(Guid Id, string NodeType, string? Name)>> GetAllNodes(Guid projectId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT id, node_type, name FROM nodes WHERE project_id = @proj", conn);
            cmd.Parameters.AddWithValue("proj", projectId);

            var results = new List<(Guid, string, string?)>();
            await using var reader = await cmd.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                results.Add((
                    reader.GetGuid(0),
                    reader.GetString(1),
                    reader.IsDBNull(2) ? null : reader.GetString(2)
                ));
            }
            return results;
        }
        finally
        {
            if (owned)
                await conn.DisposeAsync();
        }
    }

    // --- File operations (files table) ---

    /// <summary>Get or create a file record. Returns the file ID.</summary>
    public async Task<Guid> GetOrCreateFile(Guid projectId, string filePath)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            // Atomic upsert — avoids TOCTOU race from SELECT-then-INSERT
            var fileId = Guid.NewGuid();
            await using (var cmd = new NpgsqlCommand(@"
                INSERT INTO files (id, project_id, file_path) VALUES (@id, @proj, @path)
                ON CONFLICT (project_id, file_path) DO NOTHING
                RETURNING id", conn, _externalTx))
            {
                cmd.Parameters.AddWithValue("id", fileId);
                cmd.Parameters.AddWithValue("proj", projectId);
                cmd.Parameters.AddWithValue("path", filePath);
                var result = await cmd.ExecuteScalarAsync();
                if (result != null && result is not DBNull)
                    return (Guid)result;
            }

            // ON CONFLICT DO NOTHING returned no row — fetch existing
            await using (var cmd = new NpgsqlCommand(
                "SELECT id FROM files WHERE project_id = @proj AND file_path = @path", conn))
            {
                cmd.Parameters.AddWithValue("proj", projectId);
                cmd.Parameters.AddWithValue("path", filePath);
                return (Guid)(await cmd.ExecuteScalarAsync())!;
            }
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    /// <summary>Update a file's root_node_id.</summary>
    public async Task UpdateFileRootNode(Guid fileId, Guid rootNodeId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "UPDATE files SET root_node_id = @root WHERE id = @id", conn, _externalTx);
            cmd.Parameters.AddWithValue("id", fileId);
            cmd.Parameters.AddWithValue("root", rootNodeId);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    /// <summary>
    /// Delete nodes belonging to a file EXCEPT those in the keep list.
    /// Used to clean up stale code nodes when regenerating from a plan.
    /// Returns the number of deleted rows.
    /// </summary>
    public async Task<int> DeleteByFileExceptAsync(Guid fileId, List<Guid> keepIds)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            var paramNames = keepIds.Select((_, i) => $"@keep{i}").ToList();
            var inClause = paramNames.Count > 0 ? $"AND id NOT IN ({string.Join(", ", paramNames)})" : "";
            var sql = $"DELETE FROM nodes WHERE file_id = @fid {inClause};";
            await using var cmd = new NpgsqlCommand(sql, conn, _externalTx);
            cmd.Parameters.AddWithValue("fid", fileId);
            for (var i = 0; i < keepIds.Count; i++)
                cmd.Parameters.AddWithValue($"keep{i}", keepIds[i]);
            return await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    /// <summary>Delete all nodes belonging to a file. CASCADE handles attributes.</summary>
    public async Task DeleteNodesByFile(Guid fileId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "DELETE FROM nodes WHERE file_id = @file", conn, _externalTx);
            cmd.Parameters.AddWithValue("file", fileId);
            await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    /// <summary>Get all nodes for a file, ordered for tree reconstruction.</summary>
    public async Task<List<TreeNode>> GetNodesByFile(Guid fileId)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            var nodes = new List<TreeNode>();
            await using (var cmd = new NpgsqlCommand(
                "SELECT * FROM nodes WHERE file_id = @file ORDER BY parent_id NULLS FIRST, sibling_order", conn))
            {
                cmd.Parameters.AddWithValue("file", fileId);
                await using var reader = await cmd.ExecuteReaderAsync();
                while (await reader.ReadAsync())
                    nodes.Add(new TreeNode(ReadNode(reader)));
            }

            if (nodes.Count > 0)
                await LoadAttributes(conn, nodes);

            return nodes;
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    /// <summary>Delete all nodes in a project with a specific modified_by provenance. Returns count deleted.</summary>
    public async Task<int> DeleteNodesByProvenance(Guid projectId, string provenance)
    {
        var (conn, owned) = await GetConnection();
        try
        {
            await using var cmd = new NpgsqlCommand(
                "DELETE FROM nodes WHERE project_id = @proj AND modified_by = @prov", conn, _externalTx);
            cmd.Parameters.AddWithValue("proj", projectId);
            cmd.Parameters.AddWithValue("prov", provenance);
            return await cmd.ExecuteNonQueryAsync();
        }
        finally
        {
            if (owned) await conn.DisposeAsync();
        }
    }

    private static NodeRecord ReadNode(NpgsqlDataReader reader)
    {
        return new NodeRecord(
            Id: reader.GetGuid(reader.GetOrdinal("id")),
            ProjectId: reader.GetGuid(reader.GetOrdinal("project_id")),
            FileId: reader.IsDBNull(reader.GetOrdinal("file_id")) ? null : reader.GetGuid(reader.GetOrdinal("file_id")),
            NodeType: reader.GetString(reader.GetOrdinal("node_type")),
            Name: reader.IsDBNull(reader.GetOrdinal("name")) ? null : reader.GetString(reader.GetOrdinal("name")),
            Value: reader.IsDBNull(reader.GetOrdinal("value")) ? null : reader.GetString(reader.GetOrdinal("value")),
            ParentId: reader.IsDBNull(reader.GetOrdinal("parent_id")) ? null : reader.GetGuid(reader.GetOrdinal("parent_id")),
            SiblingOrder: reader.GetInt32(reader.GetOrdinal("sibling_order")),
            CreatedAt: reader.GetDateTime(reader.GetOrdinal("created_at")),
            ModifiedAt: reader.GetDateTime(reader.GetOrdinal("modified_at")),
            ModifiedBy: reader.IsDBNull(reader.GetOrdinal("modified_by")) ? null : reader.GetString(reader.GetOrdinal("modified_by"))
        );
    }
}
