// CodeStoragePoc - Apache AGE Graph Layer
//
// Manages the code_graph in AGE: syncing vertices from the relational nodes
// table, creating edges (CALLS, REFERENCES, DEPENDS_ON, MODIFIES, IMPLEMENTED_BY),
// and running Cypher queries.
//
// Supports temporal edges with valid_from/valid_to/provenance properties for
// version tracking. CloseEdge sets valid_to on open edges; CreateTemporalEdge
// creates new edges with valid_from. QueryTemporalEdges returns full history.
//
// General graph traversal via GetNeighborhood() — N-hop bidirectional traversal
// from seed nodes, used by ContextAssembler for graph-based context assembly.
//
// AGE Cypher queries are executed as raw SQL through Npgsql. Results come back
// as 'agtype' which we read as strings and parse minimally.
//
// Each connection must LOAD 'age' and SET search_path before running Cypher.
//
// Depends on: Npgsql, NodeRepository
// Used by:    Program, Vector2Seeder, PlanToCodeGenerator, ContextAssembler,
//             ChatService, ExtractionService

using Npgsql;
using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.GraphLayer;

public class AgeLayer
{
    private readonly string _connStr;

    /// <summary>
    /// Whitelist of valid AGE edge types. CreateEdge validates against this set
    /// to prevent Cypher injection via edge type parameter.
    /// </summary>
    private static readonly HashSet<string> ValidEdgeTypes = new(StringComparer.Ordinal)
    {
        // Code domain
        "CALLS", "REFERENCES", "DEPENDS_ON", "MODIFIES",
        // Ideation & planning domain
        "EXTRACTED", "SPAWNED_FROM", "IMPLEMENTED_BY", "PRODUCES",
        "CONSTRAINS", "BLOCKS", "RELATES_TO", "CONTRADICTS",
        // Research & roadmap domain
        "INFORMS",
        // Agent action domain
        "PRODUCED", "TRIGGERED", "FORKED_FROM", "OBSERVED", "INFORMED",
        // Hekate semantic edges (code analysis)
        "IMPLEMENTS", "DEFINES", "ALLOCATES"
    };

    public AgeLayer(string connectionString) => _connStr = connectionString;

    /// <summary>
    /// Escape a string value for use in Cypher string literals.
    /// Handles backslashes and single quotes.
    /// </summary>
    public static string EscapeCypher(string value)
    {
        return value.Replace("\\", "\\\\").Replace("'", "\\'");
    }

    /// <summary>
    /// Prepare a connection for AGE queries.
    /// Must be called once per connection before executing Cypher.
    /// </summary>
    private static async Task PrepareAge(NpgsqlConnection conn)
    {
        await using var load = new NpgsqlCommand("LOAD 'age';", conn);
        await load.ExecuteNonQueryAsync();

        await using var path = new NpgsqlCommand(
            "SET search_path = ag_catalog, \"$user\", public;", conn);
        await path.ExecuteNonQueryAsync();
    }

    /// <summary>
    /// Execute a Cypher query that returns a single column of agtype results.
    /// Returns the raw agtype strings.
    /// </summary>
    private async Task<List<string>> ExecuteCypher(string cypher, string returnAlias = "result")
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await PrepareAge(conn);

        // AGE requires: SELECT * FROM cypher('graph', $$ ... $$) as (alias agtype)
        // Wrap in subquery and cast agtype→text so Npgsql can read it.
        var inner = $"SELECT * FROM cypher('code_graph', $$ {cypher} $$) as ({returnAlias} agtype)";
        var sql = $"SELECT {returnAlias}::text FROM ({inner}) sub;";

        var results = new List<string>();
        await using var cmd = new NpgsqlCommand(sql, conn);
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            results.Add(reader.GetString(0));
        }
        return results;
    }

    /// <summary>
    /// Execute a Cypher query that doesn't return results (CREATE, MERGE).
    /// </summary>
    private async Task ExecuteCypherNoReturn(string cypher)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await PrepareAge(conn);

        // For void Cypher (CREATE without RETURN), AGE still needs an alias.
        // We use a dummy RETURN and discard results, or use the void form.
        // Actually, AGE requires RETURN for SELECT FROM cypher.
        // For mutations without return, we RETURN 1 as a dummy.
        var sql = $"SELECT * FROM cypher('code_graph', $$ {cypher} RETURN 1 $$) as (result agtype);";

        await using var cmd = new NpgsqlCommand(sql, conn);
        await cmd.ExecuteNonQueryAsync();
    }

    /// <summary>
    /// Sync all relational nodes into AGE as CodeNode vertices.
    /// Uses MERGE to be idempotent. Reuses a single connection for the batch.
    /// </summary>
    public async Task SyncAllVertices(NodeRepository repo, Guid projectId)
    {
        var nodes = await repo.GetAllNodes(projectId);

        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await PrepareAge(conn);

        foreach (var (id, nodeType, name) in nodes)
        {
            var safeName = EscapeCypher(name ?? "");
            var safeType = EscapeCypher(nodeType);
            var cypher = $"MERGE (n:CodeNode {{node_id: '{id}'}}) SET n.node_type = '{safeType}', n.name = '{safeName}'";
            var sql = $"SELECT * FROM cypher('code_graph', $$ {cypher} RETURN 1 $$) as (result agtype);";

            try
            {
                await using var cmd = new NpgsqlCommand(sql, conn);
                await cmd.ExecuteNonQueryAsync();
            }
            catch (PostgresException ex)
            {
                Console.WriteLine($"[GRAPH]    Warning: vertex sync failed for {name}: {ex.MessageText}");
            }
        }

        Console.WriteLine($"[SEED]     AGE vertices synced: {nodes.Count}");
    }

    /// <summary>
    /// Create a directed edge between two nodes identified by their UUIDs.
    /// </summary>
    public async Task CreateEdge(Guid fromNodeId, Guid toNodeId, string edgeType)
    {
        if (!ValidEdgeTypes.Contains(edgeType))
            throw new ArgumentException($"Invalid edge type '{edgeType}'. Must be one of: {string.Join(", ", ValidEdgeTypes)}", nameof(edgeType));

        var cypher = "MATCH (a:CodeNode {node_id: '" + fromNodeId + "'}), " +
                     "(b:CodeNode {node_id: '" + toNodeId + "'}) " +
                     "CREATE (a)-[:" + edgeType + "]->(b)";
        await ExecuteCypherNoReturn(cypher);
    }

    /// <summary>
    /// Run a Cypher query that returns node names. Used for demo queries.
    /// Returns raw agtype strings — caller parses as needed.
    /// </summary>
    public async Task<List<string>> QueryNodeNames(string cypher)
    {
        return await ExecuteCypher(cypher, "name");
    }

    /// <summary>
    /// Run a Cypher path query. Returns raw agtype path representations.
    /// </summary>
    public async Task<List<string>> QueryPaths(string cypher)
    {
        return await ExecuteCypher(cypher, "p");
    }

    /// <summary>
    /// Query: find all methods that reference a named field.
    /// </summary>
    public async Task<List<string>> FindMethodsReferencing(string fieldName)
    {
        var safeName = EscapeCypher(fieldName);
        var cypher = "MATCH (m:CodeNode)-[:REFERENCES]->(f:CodeNode {name: '" + safeName + "'}) " +
                     "WHERE m.node_type = 'method' OR m.node_type = 'constructor' " +
                     "RETURN m.name";
        return await ExecuteCypher(cypher, "name");
    }

    /// <summary>
    /// Query: find full dependency path from a named method to any field.
    /// </summary>
    public async Task<List<string>> FindDependencyPaths(string methodName)
    {
        var safeName = EscapeCypher(methodName);
        // Return endpoint names instead of path objects (AGE can't cast paths to text)
        var cypher = "MATCH (m:CodeNode {name: '" + safeName + "'})-[:REFERENCES*1..3]->(f:CodeNode) " +
                     "WHERE f.node_type = 'field' " +
                     "RETURN m.name + ' -> ' + f.name";
        return await ExecuteCypher(cypher, "p");
    }

    /// <summary>
    /// Run an arbitrary Cypher query that returns a single column.
    /// Used for ad-hoc graph queries in demos.
    /// </summary>
    internal async Task<List<string>> RunCypherQuery(string cypher)
    {
        return await ExecuteCypher(cypher, "result");
    }

    // ─── Temporal Edges ──────────────────────────────────────────────

    /// <summary>
    /// Create an edge with temporal properties (valid_from, provenance).
    /// Used for edges that track lineage — e.g., IMPLEMENTED_BY between
    /// plan tasks and code nodes. Edges start with no valid_to (open-ended).
    /// </summary>
    public async Task CreateTemporalEdge(Guid fromNodeId, Guid toNodeId,
        string edgeType, string provenance = "generator")
    {
        if (!ValidEdgeTypes.Contains(edgeType))
            throw new ArgumentException($"Invalid edge type '{edgeType}'.", nameof(edgeType));

        var now = DateTime.UtcNow.ToString("o");
        var cypher = "MATCH (a:CodeNode {node_id: '" + fromNodeId + "'}), " +
                     "(b:CodeNode {node_id: '" + toNodeId + "'}) " +
                     "CREATE (a)-[:" + edgeType +
                     " {valid_from: '" + EscapeCypher(now) +
                     "', provenance: '" + EscapeCypher(provenance) + "'}]->(b)";
        await ExecuteCypherNoReturn(cypher);
    }

    /// <summary>
    /// Close all open edges of a given type between two nodes by setting valid_to.
    /// "Open" = edges where valid_to IS NULL (property not set).
    /// </summary>
    public async Task CloseEdge(Guid fromNodeId, Guid toNodeId, string edgeType)
    {
        if (!ValidEdgeTypes.Contains(edgeType))
            throw new ArgumentException($"Invalid edge type '{edgeType}'.", nameof(edgeType));

        var now = DateTime.UtcNow.ToString("o");
        var cypher = "MATCH (a:CodeNode {node_id: '" + fromNodeId + "'})" +
                     "-[e:" + edgeType + "]->" +
                     "(b:CodeNode {node_id: '" + toNodeId + "'}) " +
                     "WHERE e.valid_to IS NULL " +
                     "SET e.valid_to = '" + EscapeCypher(now) + "'";
        await ExecuteCypherNoReturn(cypher);
    }

    /// <summary>
    /// Close all open edges of a given type originating from a node.
    /// Used when regenerating code from a plan — close all old IMPLEMENTED_BY
    /// edges before creating new ones.
    /// </summary>
    public async Task CloseAllEdgesFrom(Guid fromNodeId, string edgeType)
    {
        if (!ValidEdgeTypes.Contains(edgeType))
            throw new ArgumentException($"Invalid edge type '{edgeType}'.", nameof(edgeType));

        var now = DateTime.UtcNow.ToString("o");
        var cypher = "MATCH (a:CodeNode {node_id: '" + fromNodeId + "'})" +
                     "-[e:" + edgeType + "]->(b:CodeNode) " +
                     "WHERE e.valid_to IS NULL " +
                     "SET e.valid_to = '" + EscapeCypher(now) + "'";
        await ExecuteCypherNoReturn(cypher);
    }

    /// <summary>
    /// Query temporal edges — returns all versions (open and closed) of a given
    /// edge type from a node. Used to show version history.
    /// </summary>
    public async Task<List<string>> QueryTemporalEdges(Guid fromNodeId, string edgeType)
    {
        if (!ValidEdgeTypes.Contains(edgeType))
            throw new ArgumentException($"Invalid edge type '{edgeType}'.", nameof(edgeType));

        var cypher = "MATCH (a:CodeNode {node_id: '" + fromNodeId + "'})" +
                     "-[e:" + edgeType + "]->(b:CodeNode) " +
                     "RETURN b.name + ' [' + e.valid_from + " +
                     "CASE WHEN e.valid_to IS NULL THEN ' → current' " +
                     "ELSE ' → ' + e.valid_to END + ']'";
        return await ExecuteCypher(cypher, "result");
    }

    // ─── Live Vertex Sync ────────────────────────────────────────────

    /// <summary>
    /// Sync a single node into AGE as a CodeNode vertex.
    /// Lightweight alternative to SyncAllVertices for live use — called after
    /// StoreTurn, CreateThread, and StoreExtractedNode so new nodes are
    /// immediately queryable in the graph.
    /// </summary>
    public async Task SyncVertex(Guid nodeId, string nodeType, string? name)
    {
        var safeName = EscapeCypher(name ?? "");
        var safeType = EscapeCypher(nodeType);
        var cypher = $"MERGE (n:CodeNode {{node_id: '{nodeId}'}}) SET n.node_type = '{safeType}', n.name = '{safeName}'";

        try
        {
            await ExecuteCypherNoReturn(cypher);
        }
        catch (PostgresException ex)
        {
            Console.WriteLine($"[GRAPH]    Warning: SyncVertex failed for {nodeType} {nodeId}: {ex.MessageText}");
        }
    }

    // ─── General Graph Traversal ─────────────────────────────────────

    /// <summary>
    /// Get the neighborhood of seed nodes within N hops.
    /// Returns neighbor nodes with edge type, distance, and direction.
    /// Used by ContextAssembler to build graph-based context from resolved entities.
    /// </summary>
    public async Task<List<GraphNeighbor>> GetNeighborhood(
        IReadOnlyList<Guid> seedNodeIds, int maxHops = 2,
        HashSet<string>? edgeTypeFilter = null)
    {
        if (seedNodeIds.Count == 0)
            return new List<GraphNeighbor>();

        // Query outgoing and incoming edges separately since AGE variable-length
        // paths with undirected edges can be unreliable. We deduplicate after.
        var results = new Dictionary<Guid, GraphNeighbor>();

        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();
        await PrepareAge(conn);

        foreach (var seedId in seedNodeIds)
        {
            // Outgoing: seed -[e*1..N]-> neighbor
            await TraverseDirection(conn, seedId, maxHops, "outgoing", edgeTypeFilter, results);
            // Incoming: seed <-[e*1..N]- neighbor (seed is the target)
            await TraverseDirection(conn, seedId, maxHops, "incoming", edgeTypeFilter, results);
        }

        // Remove seeds from results (don't return the starting nodes)
        foreach (var seedId in seedNodeIds)
            results.Remove(seedId);

        // Join back to relational nodes table for full data (AGE vertices only carry id/name/type)
        var neighbors = results.Values.ToList();
        if (neighbors.Count == 0)
            return neighbors;

        var nodeIds = neighbors.Select(n => n.NodeId).ToList();
        var nodeData = await GetNodeData(conn, nodeIds);

        // Enrich with relational data
        for (int i = neighbors.Count - 1; i >= 0; i--)
        {
            var n = neighbors[i];
            if (nodeData.TryGetValue(n.NodeId, out var data))
            {
                neighbors[i] = n with
                {
                    NodeType = data.NodeType,
                    Name = data.Name,
                    Value = data.Value
                };
            }
        }

        // Sort by distance, cap at 20
        return neighbors.OrderBy(n => n.Distance).Take(20).ToList();
    }

    private async Task TraverseDirection(NpgsqlConnection conn, Guid seedId, int maxHops,
        string direction, HashSet<string>? edgeTypeFilter, Dictionary<Guid, GraphNeighbor> results)
    {
        // AGE doesn't support variable-length paths with label filters directly.
        // Query hop by hop up to maxHops.
        for (int hop = 1; hop <= maxHops; hop++)
        {
            var pathPattern = direction == "outgoing"
                ? string.Join("", Enumerable.Range(0, hop).Select(i =>
                    $"-[e{i}]->(n{i}:CodeNode)"))
                : string.Join("", Enumerable.Range(0, hop).Select(i =>
                    $"<-[e{i}]-(n{i}:CodeNode)"));

            var lastNode = $"n{hop - 1}";
            var cypher = $"MATCH (seed:CodeNode {{node_id: '{seedId}'}}){pathPattern} " +
                         $"RETURN {lastNode}.node_id, {lastNode}.name, {lastNode}.node_type, label(e{hop - 1})";

            var sql = $"SELECT r1::text, r2::text, r3::text, r4::text FROM (" +
                      $"SELECT * FROM cypher('code_graph', $$ {cypher} $$) " +
                      $"as (r1 agtype, r2 agtype, r3 agtype, r4 agtype)) sub;";

            try
            {
                await using var cmd = new NpgsqlCommand(sql, conn);
                await using var reader = await cmd.ExecuteReaderAsync();
                while (await reader.ReadAsync())
                {
                    var nodeIdStr = reader.GetString(0).Trim('"');
                    if (!Guid.TryParse(nodeIdStr, out var neighborId))
                        continue;

                    var edgeLabel = reader.GetString(3).Trim('"');

                    // Apply edge type filter
                    if (edgeTypeFilter != null && !edgeTypeFilter.Contains(edgeLabel))
                        continue;

                    // Keep shortest distance
                    if (results.TryGetValue(neighborId, out var existing) && existing.Distance <= hop)
                        continue;

                    results[neighborId] = new GraphNeighbor(
                        neighborId,
                        reader.GetString(2).Trim('"'),
                        reader.GetString(1).Trim('"'),
                        null, // Value filled from relational join
                        edgeLabel,
                        hop,
                        direction);
                }
            }
            catch (PostgresException ex)
            {
                // Variable-length paths can fail if graph is empty or vertices don't exist
                Console.WriteLine($"[GRAPH]    Traversal hop {hop} {direction} from {seedId}: {ex.MessageText}");
            }
        }
    }

    /// <summary>
    /// Fetch node data from the relational nodes table for a list of node IDs.
    /// </summary>
    private static async Task<Dictionary<Guid, (string NodeType, string? Name, string? Value)>> GetNodeData(
        NpgsqlConnection conn, List<Guid> nodeIds)
    {
        var result = new Dictionary<Guid, (string, string?, string?)>();
        if (nodeIds.Count == 0)
            return result;

        // Use ANY(@ids) for batch lookup
        await using var cmd = new NpgsqlCommand(
            "SELECT id, node_type, name, value FROM nodes WHERE id = ANY(@ids)", conn);
        cmd.Parameters.AddWithValue("ids", nodeIds.ToArray());

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var id = reader.GetGuid(0);
            var nodeType = reader.GetString(1);
            var name = reader.IsDBNull(2) ? null : reader.GetString(2);
            var value = reader.IsDBNull(3) ? null : reader.GetString(3);
            result[id] = (nodeType, name, value);
        }

        return result;
    }
}

/// <summary>
/// A neighbor node discovered via graph traversal.
/// </summary>
public record GraphNeighbor(
    Guid NodeId,
    string NodeType,
    string? Name,
    string? Value,
    string EdgeType,
    int Distance,
    string Direction);
