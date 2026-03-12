// CodeStoragePoc.Api - Pending Action Service
//
// Manages pending actions that need user approval. When a model tries to
// execute a skill above its permission level, a pending_action node is created.
// The user can approve or deny via API endpoints.
//
// Depends on: Npgsql, PermissionService
// Used by:    ChatService (tool-use loop), Api/Program.cs endpoints

using System.Text.Json;
using Npgsql;

namespace CodeStoragePoc.Api.Services;

public class PendingActionService
{
    private readonly string _connStr;
    private static readonly Guid ProjectId = new("8196b44e-6299-45a0-a5b0-bbd111f2990b");

    public PendingActionService(string connStr)
    {
        _connStr = connStr;
    }

    /// <summary>Create a pending action node under the thread, awaiting user approval.</summary>
    public async Task<Guid> CreatePendingAction(
        Guid conversationId, Guid threadId, string model, string skillName,
        string description, string paramsJson)
    {
        var nodeId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'pending_action', @name, @value, @parentId, 0, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", nodeId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("name", description);
        cmd.Parameters.AddWithValue("value", paramsJson);
        cmd.Parameters.AddWithValue("parentId", threadId);
        cmd.Parameters.AddWithValue("user", model);
        await cmd.ExecuteNonQueryAsync();

        // Store structured attributes
        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value) VALUES
            (@id, 'status', 'pending'),
            (@id, 'model', @model),
            (@id, 'skill', @skill),
            (@id, 'conversation_id', @convId)";
        attrCmd.Parameters.AddWithValue("id", nodeId);
        attrCmd.Parameters.AddWithValue("model", model);
        attrCmd.Parameters.AddWithValue("skill", skillName);
        attrCmd.Parameters.AddWithValue("convId", conversationId.ToString());
        await attrCmd.ExecuteNonQueryAsync();

        return nodeId;
    }

    /// <summary>Approve a pending action — mark as approved.</summary>
    public async Task<PendingActionDetail?> ApproveAction(Guid actionId)
    {
        var detail = await GetActionDetail(actionId);
        if (detail == null || detail.Status != "pending") return null;

        await SetStatus(actionId, "approved");
        detail = detail with { Status = "approved" };
        return detail;
    }

    /// <summary>Deny a pending action — mark as denied.</summary>
    public async Task<PendingActionDetail?> DenyAction(Guid actionId)
    {
        var detail = await GetActionDetail(actionId);
        if (detail == null || detail.Status != "pending") return null;

        await SetStatus(actionId, "denied");
        detail = detail with { Status = "denied" };
        return detail;
    }

    /// <summary>List pending actions for a conversation.</summary>
    public async Task<List<object>> ListPending(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.name, n.value, n.created_at, n.modified_by,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'skill') as skill,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'model') as model
            FROM nodes n
            WHERE n.node_type = 'pending_action'
              AND n.parent_id IN (
                  SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'
              )
            ORDER BY n.created_at DESC";
        cmd.Parameters.AddWithValue("convId", conversationId);

        var results = new List<object>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            results.Add(new
            {
                id = reader.GetGuid(0),
                description = reader.IsDBNull(1) ? null : reader.GetString(1),
                paramsJson = reader.IsDBNull(2) ? null : reader.GetString(2),
                createdAt = reader.GetDateTime(3),
                requestedBy = reader.IsDBNull(4) ? null : reader.GetString(4),
                status = reader.IsDBNull(5) ? "pending" : reader.GetString(5),
                skill = reader.IsDBNull(6) ? null : reader.GetString(6),
                model = reader.IsDBNull(7) ? null : reader.GetString(7),
            });
        }

        return results;
    }

    /// <summary>Get full detail of a pending action node.</summary>
    public async Task<PendingActionDetail?> GetActionDetail(Guid actionId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT id, name, value, parent_id, modified_by FROM nodes WHERE id = @id AND node_type = 'pending_action'";
        cmd.Parameters.AddWithValue("id", actionId);

        Guid id;
        string? description, paramsJson, model;
        Guid? threadId;

        await using (var reader = await cmd.ExecuteReaderAsync())
        {
            if (!await reader.ReadAsync()) return null;
            id = reader.GetGuid(0);
            description = reader.IsDBNull(1) ? null : reader.GetString(1);
            paramsJson = reader.IsDBNull(2) ? null : reader.GetString(2);
            threadId = reader.IsDBNull(3) ? null : reader.GetGuid(3);
            model = reader.IsDBNull(4) ? null : reader.GetString(4);
        }

        // Fetch attributes
        var attrs = new Dictionary<string, string>();
        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = "SELECT key, value FROM node_attributes WHERE node_id = @id";
        attrCmd.Parameters.AddWithValue("id", actionId);
        await using var attrReader = await attrCmd.ExecuteReaderAsync();
        while (await attrReader.ReadAsync())
            attrs[attrReader.GetString(0)] = attrReader.GetString(1);

        return new PendingActionDetail(
            id,
            description,
            attrs.GetValueOrDefault("skill", ""),
            model ?? attrs.GetValueOrDefault("model", ""),
            paramsJson,
            attrs.GetValueOrDefault("status", "pending"),
            threadId,
            attrs.TryGetValue("conversation_id", out var cid) && Guid.TryParse(cid, out var convId) ? convId : null
        );
    }

    private async Task SetStatus(Guid nodeId, string status)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value)
            VALUES (@nodeId, 'status', @status)
            ON CONFLICT (node_id, key) DO UPDATE SET value = @status";
        cmd.Parameters.AddWithValue("nodeId", nodeId);
        cmd.Parameters.AddWithValue("status", status);
        await cmd.ExecuteNonQueryAsync();
    }
}

public record PendingActionDetail(
    Guid Id,
    string? Description,
    string Skill,
    string Model,
    string? ParamsJson,
    string Status,
    Guid? ThreadId,
    Guid? ConversationId
);
