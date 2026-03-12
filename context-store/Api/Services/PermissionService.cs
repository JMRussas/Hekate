// CodeStoragePoc.Api - Permission Service
//
// Checks whether a model has permission to execute a skill in a conversation.
// Permission levels: 0 (Observe), 1 (Suggest), 2 (Assist), 3 (Auto).
// Resolution: per-model override > conversation default > system default (2).
//
// Depends on: Npgsql, SkillLoader
// Used by:    ChatService (tool-use loop), Api/Program.cs

using Npgsql;

namespace CodeStoragePoc.Api.Services;

public enum PermissionLevel
{
    Observe = 0,
    Suggest = 1,
    Assist = 2,
    Auto = 3
}

public enum PermissionResult
{
    Allowed,
    NeedsApproval,
    Denied
}

public class PermissionService
{
    private readonly string _connStr;
    private readonly SkillLoader _skillLoader;
    private const PermissionLevel SystemDefault = PermissionLevel.Assist;

    public PermissionService(string connStr, SkillLoader skillLoader)
    {
        _connStr = connStr;
        _skillLoader = skillLoader;
    }

    /// <summary>Check whether the given model can execute the skill in this conversation.</summary>
    public async Task<PermissionResult> CheckPermission(Guid? conversationId, string model, string skillName)
    {
        var requiredLevel = GetSkillPermissionLevel(skillName);
        var effectiveLevel = await GetEffectiveLevel(conversationId, model);

        if (effectiveLevel >= requiredLevel)
            return PermissionResult.Allowed;

        // If the model's level is one below required and >= Suggest, it can propose
        if (effectiveLevel >= PermissionLevel.Suggest)
            return PermissionResult.NeedsApproval;

        return PermissionResult.Denied;
    }

    /// <summary>Get the required permission level for a skill from skills.json.</summary>
    public PermissionLevel GetSkillPermissionLevel(string skillName)
    {
        var level = _skillLoader.GetSkillPermissionLevel(skillName);
        return level.HasValue ? (PermissionLevel)level.Value : PermissionLevel.Assist;
    }

    /// <summary>Resolve the effective permission level for a model in a conversation.
    /// Priority: per-model override > conversation default > system default.</summary>
    public async Task<PermissionLevel> GetEffectiveLevel(Guid? conversationId, string model)
    {
        if (!conversationId.HasValue)
            return SystemDefault;

        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Fetch conversation-level permission attributes in one query
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT key, value FROM node_attributes
            WHERE node_id = @convId
              AND key IN ('permission_level', @modelKey)";
        cmd.Parameters.AddWithValue("convId", conversationId.Value);
        cmd.Parameters.AddWithValue("modelKey", $"permission:{model}");

        string? convDefault = null;
        string? modelOverride = null;

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var key = reader.GetString(0);
            var value = reader.GetString(1);
            if (key == $"permission:{model}")
                modelOverride = value;
            else if (key == "permission_level")
                convDefault = value;
        }

        // Resolution: per-model > conversation > system
        if (modelOverride != null && int.TryParse(modelOverride, out var ml))
            return (PermissionLevel)Math.Clamp(ml, 0, 3);

        if (convDefault != null && int.TryParse(convDefault, out var cl))
            return (PermissionLevel)Math.Clamp(cl, 0, 3);

        return SystemDefault;
    }

    /// <summary>Set the conversation-level default permission.</summary>
    public async Task SetConversationPermission(Guid conversationId, PermissionLevel level)
    {
        await SetAttribute(conversationId, "permission_level", ((int)level).ToString());
    }

    /// <summary>Set a per-model permission override for a conversation.</summary>
    public async Task SetModelPermission(Guid conversationId, string model, PermissionLevel level)
    {
        await SetAttribute(conversationId, $"permission:{model}", ((int)level).ToString());
    }

    /// <summary>Get the current permission config for a conversation.</summary>
    public async Task<object> GetPermissionConfig(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT key, value FROM node_attributes
            WHERE node_id = @convId
              AND (key = 'permission_level' OR key LIKE 'permission:%')";
        cmd.Parameters.AddWithValue("convId", conversationId);

        int defaultLevel = (int)SystemDefault;
        var modelOverrides = new Dictionary<string, int>();

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var key = reader.GetString(0);
            var value = reader.GetString(1);
            if (key == "permission_level" && int.TryParse(value, out var dl))
                defaultLevel = Math.Clamp(dl, 0, 3);
            else if (key.StartsWith("permission:") && int.TryParse(value, out var ml))
                modelOverrides[key["permission:".Length..]] = Math.Clamp(ml, 0, 3);
        }

        return new
        {
            defaultLevel,
            defaultLabel = ((PermissionLevel)defaultLevel).ToString(),
            modelOverrides,
            systemDefault = (int)SystemDefault
        };
    }

    private async Task SetAttribute(Guid nodeId, string key, string value)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value)
            VALUES (@nodeId, @key, @value)
            ON CONFLICT (node_id, key) DO UPDATE SET value = @value";
        cmd.Parameters.AddWithValue("nodeId", nodeId);
        cmd.Parameters.AddWithValue("key", key);
        cmd.Parameters.AddWithValue("value", value);
        await cmd.ExecuteNonQueryAsync();
    }
}
