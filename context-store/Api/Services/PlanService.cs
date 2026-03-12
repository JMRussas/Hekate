// CodeStoragePoc.Api - Plan Service
//
// Queries plan nodes from the DB and converts them to DTOs for the frontend.
// Uses NodeRepository.GetSubtree() for full plan tree, direct SQL for plan list
// (since GetAllNodes returns lightweight tuples without attributes).
//
// Depends on: NodeRepository, Npgsql
// Used by:    Api/Program.cs endpoints

using CodeStoragePoc.ContextRouter;
using CodeStoragePoc.DbLayer;
using Npgsql;

namespace CodeStoragePoc.Api.Services;

// --- DTOs ---

public record PlanSummary(
    Guid Id,
    string? Name,
    string? Value,
    string? PlanType,
    string? Status,
    string? Priority,
    string? TargetDate,
    int PhaseCount,
    DateTime CreatedAt);

public record PlanNodeDto(
    Guid Id,
    string NodeType,
    string? Name,
    string? Value,
    string? Status,
    Dictionary<string, string> Attributes,
    List<PlanNodeDto> Children);

// --- Service ---

public class PlanService
{
    private readonly NodeRepository _repo;
    private readonly string _connStr;

    public PlanService(NodeRepository repo, string connStr)
    {
        _repo = repo;
        _connStr = connStr;
    }

    /// <summary>List all plans in the project with key attributes.</summary>
    public async Task<List<PlanSummary>> ListPlans()
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Get all plan nodes with their attributes in one query
        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.name, n.value, n.created_at,
                   a_type.value AS plan_type,
                   a_status.value AS status,
                   a_priority.value AS priority,
                   a_target.value AS target_date,
                   (SELECT COUNT(*) FROM nodes c
                    WHERE c.parent_id = n.id AND c.node_type = @phaseType) AS phase_count
            FROM nodes n
            LEFT JOIN node_attributes a_type
                ON a_type.node_id = n.id AND a_type.key = 'plan_type'
            LEFT JOIN node_attributes a_status
                ON a_status.node_id = n.id AND a_status.key = 'status'
            LEFT JOIN node_attributes a_priority
                ON a_priority.node_id = n.id AND a_priority.key = 'priority'
            LEFT JOIN node_attributes a_target
                ON a_target.node_id = n.id AND a_target.key = 'target_date'
            WHERE n.node_type = @planType
            ORDER BY n.created_at DESC
        """, conn);
        cmd.Parameters.AddWithValue("planType", NodeTypes.Plan);
        cmd.Parameters.AddWithValue("phaseType", NodeTypes.PlanPhase);

        var plans = new List<PlanSummary>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            plans.Add(new PlanSummary(
                Id: reader.GetGuid(0),
                Name: reader.IsDBNull(1) ? null : reader.GetString(1),
                Value: reader.IsDBNull(2) ? null : reader.GetString(2),
                CreatedAt: reader.GetDateTime(3),
                PlanType: reader.IsDBNull(4) ? null : reader.GetString(4),
                Status: reader.IsDBNull(5) ? null : reader.GetString(5),
                Priority: reader.IsDBNull(6) ? null : reader.GetString(6),
                TargetDate: reader.IsDBNull(7) ? null : reader.GetString(7),
                PhaseCount: reader.GetInt32(8)
            ));
        }

        return plans;
    }

    /// <summary>Get a full plan tree with all descendants converted to DTOs.</summary>
    public async Task<PlanNodeDto?> GetPlanTree(Guid planId)
    {
        var tree = await _repo.GetSubtree(planId);
        if (tree == null) return null;

        return ConvertToDto(tree);
    }

    private static PlanNodeDto ConvertToDto(TreeNode node)
    {
        var children = node.Children
            .Select(ConvertToDto)
            .ToList();

        return new PlanNodeDto(
            Id: node.Record.Id,
            NodeType: node.Record.NodeType,
            Name: node.Record.Name,
            Value: node.Record.Value,
            Status: node.Attr("status") is { Length: > 0 } s ? s : null,
            Attributes: new Dictionary<string, string>(node.Attributes),
            Children: children);
    }
}
