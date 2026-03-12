// CodeStoragePoc.Api - Node Service
//
// Provides node detail, children, breadcrumb, and graph edge queries
// for the workspace view. Supports browsing any node in the tree.
//
// Depends on: Npgsql, AgeLayer (for graph edge queries)
// Used by:    Api/Program.cs endpoints

using System.Text.Json;
using CodeStoragePoc.GraphLayer;
using Npgsql;

namespace CodeStoragePoc.Api.Services;

public class NodeService
{
    private readonly string _connStr;

    public NodeService(string connStr)
    {
        _connStr = connStr;
    }

    /// <summary>Get full detail for a node: fields, attributes, breadcrumb.</summary>
    public async Task<object?> GetNodeDetail(Guid nodeId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Node fields
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT id, project_id, node_type, name, value, parent_id,
                   sibling_order, created_at, modified_at, modified_by
            FROM nodes WHERE id = @id";
        cmd.Parameters.AddWithValue("id", nodeId);

        object? node;
        Guid? parentId;
        await using (var reader = await cmd.ExecuteReaderAsync())
        {
            if (!await reader.ReadAsync()) return null;

            parentId = reader.IsDBNull(5) ? null : reader.GetGuid(5);
            node = new
            {
                id = reader.GetGuid(0),
                projectId = reader.GetGuid(1),
                nodeType = reader.GetString(2),
                name = reader.IsDBNull(3) ? null : reader.GetString(3),
                value = reader.IsDBNull(4) ? null : reader.GetString(4),
                parentId,
                siblingOrder = reader.GetInt32(6),
                createdAt = reader.GetDateTime(7),
                modifiedAt = reader.GetDateTime(8),
                modifiedBy = reader.IsDBNull(9) ? null : reader.GetString(9),
            };
        }

        // Attributes
        var attributes = new Dictionary<string, string>();
        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = "SELECT key, value FROM node_attributes WHERE node_id = @id";
        attrCmd.Parameters.AddWithValue("id", nodeId);
        await using (var attrReader = await attrCmd.ExecuteReaderAsync())
        {
            while (await attrReader.ReadAsync())
                attributes[attrReader.GetString(0)] = attrReader.GetString(1);
        }

        // Breadcrumb — walk parent chain up to root
        var breadcrumb = await GetBreadcrumb(conn, parentId);

        // Child count
        await using var countCmd = conn.CreateCommand();
        countCmd.CommandText = "SELECT COUNT(*) FROM nodes WHERE parent_id = @id";
        countCmd.Parameters.AddWithValue("id", nodeId);
        var childCount = (long)(await countCmd.ExecuteScalarAsync())!;

        return new { node, attributes, breadcrumb, childCount };
    }

    /// <summary>Get ordered children of a node with their attributes.</summary>
    public async Task<List<object>> GetNodeChildren(Guid nodeId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 120) as summary,
                   n.sibling_order, n.created_at, n.modified_at,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status
            FROM nodes n
            WHERE n.parent_id = @id
            ORDER BY n.sibling_order, n.created_at";
        cmd.Parameters.AddWithValue("id", nodeId);

        var children = new List<object>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            children.Add(new
            {
                id = reader.GetGuid(0),
                nodeType = reader.GetString(1),
                name = reader.IsDBNull(2) ? null : reader.GetString(2),
                summary = reader.IsDBNull(3) ? null : reader.GetString(3),
                siblingOrder = reader.GetInt32(4),
                createdAt = reader.GetDateTime(5),
                modifiedAt = reader.GetDateTime(6),
                status = reader.IsDBNull(7) ? null : reader.GetString(7),
            });
        }

        return children;
    }

    /// <summary>Get graph edges (AGE) connected to a node in both directions.</summary>
    public async Task<List<object>> GetNodeEdges(Guid nodeId)
    {
        var edges = new List<object>();

        try
        {
            await using var conn = new NpgsqlConnection(_connStr);
            await conn.OpenAsync();

            // AGE requires LOAD + search_path per connection
            await using var load = new NpgsqlCommand("LOAD 'age';", conn);
            await load.ExecuteNonQueryAsync();
            await using var path = new NpgsqlCommand(
                "SET search_path = ag_catalog, \"$user\", public;", conn);
            await path.ExecuteNonQueryAsync();

            // Query outgoing edges (sanitize nodeId for Cypher)
            var safeId = AgeLayer.EscapeCypher(nodeId.ToString());
            var outCypher = $"MATCH (a:CodeNode {{node_id: '{safeId}'}})-[r]->(b:CodeNode) RETURN type(r), b.node_id, b.name, b.node_type";
            var outSql = $"SELECT result::text FROM (SELECT * FROM cypher('code_graph', $$ {outCypher} $$) as (result agtype)) sub;";
            await using var outCmd = new NpgsqlCommand(outSql, conn);
            await using (var reader = await outCmd.ExecuteReaderAsync())
            {
                while (await reader.ReadAsync())
                {
                    var raw = reader.GetString(0);
                    edges.Add(ParseEdgeResult(raw, "outgoing"));
                }
            }

            // Query incoming edges
            var inCypher = $"MATCH (a:CodeNode)-[r]->(b:CodeNode {{node_id: '{safeId}'}}) RETURN type(r), a.node_id, a.name, a.node_type";
            var inSql = $"SELECT result::text FROM (SELECT * FROM cypher('code_graph', $$ {inCypher} $$) as (result agtype)) sub;";
            await using var inCmd = new NpgsqlCommand(inSql, conn);
            await using (var reader = await inCmd.ExecuteReaderAsync())
            {
                while (await reader.ReadAsync())
                {
                    var raw = reader.GetString(0);
                    edges.Add(ParseEdgeResult(raw, "incoming"));
                }
            }
        }
        catch (Exception ex)
        {
            // AGE may not be available — return empty edges gracefully
            Console.WriteLine($"[WARN] AGE edge query failed: {ex.Message}");
        }

        return edges;
    }

    /// <summary>Get all root-level nodes (no parent) across all projects, for tree navigation.</summary>
    public async Task<List<object>> GetRootNodes()
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.node_type, n.name, n.project_id, p.name as project_name,
                   n.created_at, n.modified_at,
                   (SELECT COUNT(*) FROM nodes c WHERE c.parent_id = n.id) as child_count
            FROM nodes n
            LEFT JOIN projects p ON p.id = n.project_id
            WHERE n.parent_id IS NULL
            ORDER BY n.modified_at DESC";

        var roots = new List<object>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            roots.Add(new
            {
                id = reader.GetGuid(0),
                nodeType = reader.GetString(1),
                name = reader.IsDBNull(2) ? null : reader.GetString(2),
                projectId = reader.GetGuid(3),
                projectName = reader.IsDBNull(4) ? null : reader.GetString(4),
                createdAt = reader.GetDateTime(5),
                modifiedAt = reader.GetDateTime(6),
                childCount = reader.GetInt64(7),
            });
        }

        return roots;
    }

    /// <summary>Update a node's name and value explicitly (allows nulling).</summary>
    public async Task UpdateNodeFull(Guid nodeId, string? name, string? value)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            UPDATE nodes SET
                name = @name,
                value = @val,
                modified_by = 'user',
                modified_at = now()
            WHERE id = @id";
        cmd.Parameters.AddWithValue("id", nodeId);
        cmd.Parameters.AddWithValue("name", (object?)name ?? DBNull.Value);
        cmd.Parameters.AddWithValue("val", (object?)value ?? DBNull.Value);
        await cmd.ExecuteNonQueryAsync();
    }

    /// <summary>Replace all attributes for a node in a single transaction.</summary>
    public async Task UpdateAttributes(Guid nodeId, Dictionary<string, string> attributes)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();

        try
        {
            await using (var delCmd = conn.CreateCommand())
            {
                delCmd.Transaction = tx;
                delCmd.CommandText = "DELETE FROM node_attributes WHERE node_id = @id";
                delCmd.Parameters.AddWithValue("id", nodeId);
                await delCmd.ExecuteNonQueryAsync();
            }

            foreach (var kvp in attributes)
            {
                await using (var insCmd = conn.CreateCommand())
                {
                    insCmd.Transaction = tx;
                    insCmd.CommandText = "INSERT INTO node_attributes (id, node_id, key, value) VALUES (gen_random_uuid(), @id, @key, @val)";
                    insCmd.Parameters.AddWithValue("id", nodeId);
                    insCmd.Parameters.AddWithValue("key", kvp.Key);
                    insCmd.Parameters.AddWithValue("val", kvp.Value);
                    await insCmd.ExecuteNonQueryAsync();
                }
            }

            await tx.CommitAsync();
        }
        catch
        {
            await tx.RollbackAsync();
            throw;
        }
    }

    /// <summary>Create a new child node with inherited project context and optional attributes.</summary>
    public async Task<Guid> CreateChildNode(Guid parentId, string nodeType, string? name, string? value, Dictionary<string, string>? attributes = null)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();

        try
        {
            Guid projectId;
            await using (var cmd = conn.CreateCommand())
            {
                cmd.Transaction = tx;
                cmd.CommandText = "SELECT project_id FROM nodes WHERE id = @id";
                cmd.Parameters.AddWithValue("id", parentId);
                var result = await cmd.ExecuteScalarAsync();
                if (result == null) throw new Exception("Parent node not found");
                projectId = (Guid)result;
            }

            int siblingOrder;
            await using (var cmd = conn.CreateCommand())
            {
                cmd.Transaction = tx;
                cmd.CommandText = "SELECT COALESCE(MAX(sibling_order), 0) + 100 FROM nodes WHERE parent_id = @id";
                cmd.Parameters.AddWithValue("id", parentId);
                siblingOrder = Convert.ToInt32(await cmd.ExecuteScalarAsync());
            }

            var nodeId = Guid.NewGuid();
            await using (var cmd = conn.CreateCommand())
            {
                cmd.Transaction = tx;
                cmd.CommandText = @"
                    INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by)
                    VALUES (@id, @proj, @type, @name, @val, @parent, @order, 'user')";
                cmd.Parameters.AddWithValue("id", nodeId);
                cmd.Parameters.AddWithValue("proj", projectId);
                cmd.Parameters.AddWithValue("type", nodeType);
                cmd.Parameters.AddWithValue("name", (object?)name ?? DBNull.Value);
                cmd.Parameters.AddWithValue("val", (object?)value ?? DBNull.Value);
                cmd.Parameters.AddWithValue("parent", parentId);
                cmd.Parameters.AddWithValue("order", siblingOrder);
                await cmd.ExecuteNonQueryAsync();
            }

            if (attributes != null)
            {
                foreach (var kvp in attributes)
                {
                    await using (var cmd = conn.CreateCommand())
                    {
                        cmd.Transaction = tx;
                        cmd.CommandText = "INSERT INTO node_attributes (id, node_id, key, value) VALUES (gen_random_uuid(), @id, @key, @val)";
                        cmd.Parameters.AddWithValue("id", nodeId);
                        cmd.Parameters.AddWithValue("key", kvp.Key);
                        cmd.Parameters.AddWithValue("val", kvp.Value);
                        await cmd.ExecuteNonQueryAsync();
                    }
                }
            }

            await tx.CommitAsync();
            return nodeId;
        }
        catch
        {
            await tx.RollbackAsync();
            throw;
        }
    }

    /// <summary>Get the latest interpretation node for a conversation (for debug panel on load).</summary>
    public async Task<object?> GetLatestInterpretation(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.name, n.value, n.created_at
            FROM nodes n
            WHERE n.node_type = 'interpretation'
              AND n.parent_id IN (
                  SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'
              )
            ORDER BY n.created_at DESC
            LIMIT 1";
        cmd.Parameters.AddWithValue("convId", conversationId);

        await using var reader = await cmd.ExecuteReaderAsync();
        if (!await reader.ReadAsync()) return null;

        var nodeId = reader.GetGuid(0);
        reader.Close();

        // Fetch attributes
        var attrs = new Dictionary<string, string>();
        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = "SELECT key, value FROM node_attributes WHERE node_id = @id";
        attrCmd.Parameters.AddWithValue("id", nodeId);
        await using var attrReader = await attrCmd.ExecuteReaderAsync();
        while (await attrReader.ReadAsync())
            attrs[attrReader.GetString(0)] = attrReader.GetString(1);

        // Build debug-compatible shape
        var entities = new List<object>();
        if (attrs.TryGetValue("entities", out var entJson))
        {
            try
            {
                var arr = JsonSerializer.Deserialize<JsonElement>(entJson);
                if (arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (var el in arr.EnumerateArray())
                    {
                        entities.Add(new
                        {
                            mention = el.TryGetProperty("mention", out var m) ? m.GetString() : "",
                            nodeId = el.TryGetProperty("nodeId", out var nid) ? nid.GetString() : null,
                            nodeName = el.TryGetProperty("nodeName", out var nn) ? nn.GetString() : null,
                            nodeType = el.TryGetProperty("nodeType", out var nt) ? nt.GetString() : null,
                        });
                    }
                }
            }
            catch { }
        }

        return new
        {
            interpret = new
            {
                intent = attrs.GetValueOrDefault("intent", ""),
                confidence = double.TryParse(attrs.GetValueOrDefault("confidence", "0"), out var c) ? c : 0,
                reasoning = attrs.GetValueOrDefault("reasoning"),
                project = new
                {
                    type = attrs.GetValueOrDefault("project_type", "existing"),
                    name = attrs.GetValueOrDefault("project_name"),
                    id = attrs.GetValueOrDefault("project_id"),
                },
                entities,
                isRegexFallback = attrs.GetValueOrDefault("is_regex_fallback", "False") == "True",
                durationMs = int.TryParse(attrs.GetValueOrDefault("duration_ms", "0"), out var d) ? d : 0,
            }
        };
    }

    /// <summary>Walk parent chain to build breadcrumb using a recursive CTE (single query).</summary>
    private static async Task<List<object>> GetBreadcrumb(NpgsqlConnection conn, Guid? startParentId)
    {
        if (!startParentId.HasValue) return new List<object>();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            WITH RECURSIVE ancestors AS (
                SELECT id, node_type, name, parent_id, 0 AS depth
                FROM nodes WHERE id = @startId
                UNION ALL
                SELECT n.id, n.node_type, n.name, n.parent_id, a.depth + 1
                FROM nodes n
                JOIN ancestors a ON n.id = a.parent_id
                WHERE a.depth < 10
            )
            SELECT id, node_type, name FROM ancestors
            ORDER BY depth DESC";
        cmd.Parameters.AddWithValue("startId", startParentId.Value);

        var crumbs = new List<object>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            crumbs.Add(new
            {
                id = reader.GetGuid(0),
                nodeType = reader.GetString(1),
                name = reader.IsDBNull(2) ? null : reader.GetString(2),
            });
        }
        return crumbs;
    }

    /// <summary>Parse AGE Cypher result row into an edge object.</summary>
    private static object ParseEdgeResult(string raw, string direction)
    {
        // AGE returns results as agtype — for multi-column RETURN, it comes as a JSON-like array
        // Simple parsing: strip outer brackets and split
        // Format varies but typically: ["EDGE_TYPE", "uuid", "name", "node_type"]
        try
        {
            // Try parsing as JSON array
            var arr = JsonSerializer.Deserialize<JsonElement>(raw);
            if (arr.ValueKind == JsonValueKind.Array)
            {
                var elements = new List<string>();
                foreach (var el in arr.EnumerateArray())
                    elements.Add(el.GetString() ?? el.ToString());

                return new
                {
                    edgeType = elements.ElementAtOrDefault(0) ?? "UNKNOWN",
                    targetId = elements.ElementAtOrDefault(1),
                    targetName = elements.ElementAtOrDefault(2),
                    targetType = elements.ElementAtOrDefault(3),
                    direction,
                };
            }
        }
        catch { }

        // Fallback — return raw
        return new { edgeType = "UNKNOWN", targetId = (string?)null, targetName = raw, targetType = (string?)null, direction };
    }
}
