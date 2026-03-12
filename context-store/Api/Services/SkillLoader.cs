// CodeStoragePoc.Api - Skill Loader
//
// Reads skill definitions from tools/skills/skills.json.
// Re-reads the file on each call for hot reload during development.
//
// Depends on: System.Text.Json
// Used by:    ChatService, PermissionService

using System.Text.Json;

namespace CodeStoragePoc.Api.Services;

public record SkillDefinition(string Name, string Description, string Handler, int? PermissionLevel, JsonElement Parameters);

public class SkillLoader
{
    private readonly string _skillsPath;

    public SkillLoader(string skillsPath)
    {
        _skillsPath = skillsPath;
    }

    /// <summary>
    /// Load raw skill definitions from skills.json.
    /// Re-reads the file each time for hot reload.
    /// </summary>
    public List<SkillDefinition> LoadSkillDefinitions()
    {
        if (!File.Exists(_skillsPath))
            return new List<SkillDefinition>();

        var json = File.ReadAllText(_skillsPath);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        var skills = new List<SkillDefinition>();
        foreach (var skill in root.GetProperty("skills").EnumerateArray())
        {
            int? permLevel = skill.TryGetProperty("permissionLevel", out var pl) ? pl.GetInt32() : null;
            skills.Add(new SkillDefinition(
                skill.GetProperty("name").GetString()!,
                skill.GetProperty("description").GetString()!,
                skill.GetProperty("handler").GetString()!,
                permLevel,
                skill.TryGetProperty("parameters", out var p) ? p.Clone() : default
            ));
        }

        return skills;
    }

    /// <summary>Get the required permission level for a skill by name. Returns null if skill not found.</summary>
    public int? GetSkillPermissionLevel(string skillName)
    {
        var skills = LoadSkillDefinitions();
        var skill = skills.FirstOrDefault(s => s.Name == skillName);
        return skill?.PermissionLevel;
    }

    public List<string> GetSkillNames()
    {
        return LoadSkillDefinitions().Select(s => s.Name).ToList();
    }
}
