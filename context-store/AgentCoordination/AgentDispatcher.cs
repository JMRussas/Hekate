// CodeStoragePoc - Agent Dispatcher
//
// Event-driven agent spawning. Listens for DB node_changed notifications,
// matches changed nodes against trigger rules, spawns CLI agents with
// assembled context, and pushes results back via SystemMessageBus.
//
// Two-way agent flow:
//   Inbound:  Models query context (ContextAssembler) and store turns (ChatService)
//   Outbound: System proactively spawns agents when interesting things happen (this)
//
// Trigger rules are loaded from tools/skills/triggers.json (hot-reloaded).
// Each rule maps a node_type + condition to an agent (CLI model) + prompt template.
//
// Depends on: Npgsql, NodeRepository, ContextAssembler, SystemMessageBus
// Used by:    Api/Program.cs (hosted service)

using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using CodeStoragePoc.DbLayer;
using Npgsql;

namespace CodeStoragePoc.AgentCoordination;

public class AgentDispatcher : IAsyncDisposable
{
    private readonly string _connStr;
    private readonly NodeRepository _repo;
    private readonly Action<string, string>? _onResult; // (level, text) → push to SystemMessageBus
    private NpgsqlConnection? _conn;
    private CancellationTokenSource? _cts;
    private Task? _listenerTask;

    // Debounce: don't re-trigger on the same node within this window
    private readonly Dictionary<Guid, DateTime> _recentlyTriggered = new();
    private static readonly TimeSpan DebounceWindow = TimeSpan.FromSeconds(30);

    // Trigger rules loaded from config
    private List<TriggerRule> _rules = new();
    private DateTime _rulesLoadedAt = DateTime.MinValue;
    private static readonly string TriggersPath = Path.Combine("tools", "skills", "triggers.json");

    public AgentDispatcher(string connectionString, NodeRepository repo, Action<string, string>? onResult = null)
    {
        _connStr = connectionString;
        _repo = repo;
        _onResult = onResult;
    }

    /// <summary>
    /// Start listening for node_changed events and dispatching agents.
    /// </summary>
    public async Task StartAsync()
    {
        ReloadRules();

        _conn = new NpgsqlConnection(_connStr);
        await _conn.OpenAsync();

        _conn.Notification += async (_, args) =>
        {
            try
            {
                await HandleNotification(args.Payload);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DISPATCH] Error handling notification: {ex.Message}");
            }
        };

        await using var cmd = new NpgsqlCommand("LISTEN node_changed", _conn);
        await cmd.ExecuteNonQueryAsync();

        _cts = new CancellationTokenSource();
        var token = _cts.Token;
        _listenerTask = Task.Run(async () =>
        {
            try
            {
                while (!token.IsCancellationRequested)
                    await _conn.WaitAsync(token);
            }
            catch (OperationCanceledException) { }
        }, token);

        Console.WriteLine($"[DISPATCH] Agent dispatcher started, {_rules.Count} trigger rules loaded");
    }

    private async Task HandleNotification(string payload)
    {
        // Payload is the node ID from the pg_notify trigger
        if (!Guid.TryParse(payload, out var nodeId))
            return;

        // Debounce
        lock (_recentlyTriggered)
        {
            var now = DateTime.UtcNow;
            // Clean old entries
            var stale = _recentlyTriggered.Where(kv => now - kv.Value > DebounceWindow).Select(kv => kv.Key).ToList();
            foreach (var key in stale) _recentlyTriggered.Remove(key);

            if (_recentlyTriggered.ContainsKey(nodeId))
                return;
            _recentlyTriggered[nodeId] = now;
        }

        // Hot-reload rules if file changed
        ReloadRules();

        // Fetch the changed node
        var node = await GetNodeInfo(nodeId);
        if (node == null) return;

        // Match against trigger rules
        foreach (var rule in _rules)
        {
            if (!rule.Enabled) continue;
            if (!MatchesRule(rule, node)) continue;

            Console.WriteLine($"[DISPATCH] Trigger '{rule.Name}' matched on [{node.NodeType}] {node.Name ?? "(unnamed)"}");

            // Spawn agent in background (don't block the notification handler)
            _ = Task.Run(() => SpawnAgent(rule, node));
        }
    }

    private bool MatchesRule(TriggerRule rule, NodeInfo node)
    {
        // Node type must match
        if (!rule.NodeTypes.Contains(node.NodeType, StringComparer.OrdinalIgnoreCase))
            return false;

        // Optional condition: check attribute values
        if (rule.Conditions != null)
        {
            foreach (var (key, pattern) in rule.Conditions)
            {
                if (!node.Attributes.TryGetValue(key, out var value))
                    return false;
                if (!Regex.IsMatch(value, pattern, RegexOptions.IgnoreCase))
                    return false;
            }
        }

        return true;
    }

    private async Task SpawnAgent(TriggerRule rule, NodeInfo node)
    {
        try
        {
            // Build the prompt from the template + node context
            var prompt = BuildPrompt(rule, node);

            // Select CLI + args based on provider
            var (executable, args) = GetCliCommand(rule.Agent, prompt);

            Console.WriteLine($"[DISPATCH] Spawning {rule.Agent} for trigger '{rule.Name}'...");

            var resolvedExe = CliResolver.Resolve(executable);

            var psi = new ProcessStartInfo
            {
                FileName = resolvedExe,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            foreach (var arg in args)
                psi.ArgumentList.Add(arg);

            using var proc = Process.Start(psi);
            if (proc == null)
            {
                Console.WriteLine($"[DISPATCH] Failed to start {executable}");
                return;
            }

            // Pipe prompt to stdin
            await proc.StandardInput.WriteAsync(prompt);
            proc.StandardInput.Close();

            var stdoutTask = proc.StandardOutput.ReadToEndAsync();
            var stderrTask = proc.StandardError.ReadToEndAsync();
            await Task.WhenAll(stdoutTask, stderrTask);
            await proc.WaitForExitAsync();

            var result = stdoutTask.Result;
            if (proc.ExitCode != 0)
            {
                var stderr = stderrTask.Result;
                Console.WriteLine($"[DISPATCH] {executable} failed (exit {proc.ExitCode}): {stderr.Trim()}");
                return;
            }

            // Truncate if too long
            var displayResult = result.Length > 500 ? result[..500] + "..." : result;
            Console.WriteLine($"[DISPATCH] '{rule.Name}' completed ({result.Length} chars)");

            // Push result to chat UI via SystemMessageBus
            _onResult?.Invoke("info", $"**[{rule.Name}]** ({rule.Agent})\n{displayResult}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DISPATCH] Agent spawn failed: {ex.Message}");
        }
    }

    private string BuildPrompt(TriggerRule rule, NodeInfo node)
    {
        var sb = new StringBuilder();
        sb.AppendLine("<context>");
        sb.AppendLine($"  <trigger>{rule.Name}</trigger>");
        sb.AppendLine($"  <node_type>{node.NodeType}</node_type>");
        sb.AppendLine($"  <node_name>{node.Name ?? "(unnamed)"}</node_name>");

        if (node.Value != null)
        {
            var value = node.Value.Length > 500 ? node.Value[..500] + "..." : node.Value;
            sb.AppendLine($"  <node_value>{value}</node_value>");
        }

        if (node.ParentType != null)
            sb.AppendLine($"  <parent type=\"{node.ParentType}\">{node.ParentName ?? "(unnamed)"}</parent>");

        if (node.Attributes.Count > 0)
        {
            sb.AppendLine("  <attributes>");
            foreach (var (key, val) in node.Attributes)
                sb.AppendLine($"    <{key}>{val}</{key}>");
            sb.AppendLine("  </attributes>");
        }

        sb.AppendLine("</context>");
        sb.AppendLine();
        sb.AppendLine(rule.PromptTemplate);

        return sb.ToString();
    }

    private static (string Executable, List<string> Args) GetCliCommand(string agent, string prompt)
    {
        return agent.ToLowerInvariant() switch
        {
            "claude" or "sonnet" or "opus" or "haiku" =>
                ("claude", new List<string> { "--print", "-", "--output-format", "text" }),
            "gemini" or "flash" or "pro" =>
                ("gemini", new List<string> { "-p", "-" }),
            "codex" or "gpt" =>
                ("codex", new List<string> { "exec", "--" }),
            _ => ("claude", new List<string> { "--print", "-", "--output-format", "text" })
        };
    }

    private void ReloadRules()
    {
        try
        {
            if (!File.Exists(TriggersPath)) return;

            var lastWrite = File.GetLastWriteTimeUtc(TriggersPath);
            if (lastWrite <= _rulesLoadedAt) return;

            var json = File.ReadAllText(TriggersPath);
            var rules = JsonSerializer.Deserialize<TriggerConfig>(json,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });

            if (rules?.Triggers != null)
            {
                _rules = rules.Triggers;
                _rulesLoadedAt = lastWrite;
                Console.WriteLine($"[DISPATCH] Reloaded {_rules.Count} trigger rules");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DISPATCH] Failed to load triggers: {ex.Message}");
        }
    }

    private async Task<NodeInfo?> GetNodeInfo(Guid nodeId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 1000),
                   p.node_type, p.name, n.modified_by
            FROM nodes n
            LEFT JOIN nodes p ON n.parent_id = p.id
            WHERE n.id = @id
        """, conn);
        cmd.Parameters.AddWithValue("id", nodeId);

        await using var reader = await cmd.ExecuteReaderAsync();
        if (!await reader.ReadAsync()) return null;

        var info = new NodeInfo
        {
            Id = reader.GetGuid(0),
            NodeType = reader.GetString(1),
            Name = reader.IsDBNull(2) ? null : reader.GetString(2),
            Value = reader.IsDBNull(3) ? null : reader.GetString(3),
            ParentType = reader.IsDBNull(4) ? null : reader.GetString(4),
            ParentName = reader.IsDBNull(5) ? null : reader.GetString(5),
            ModifiedBy = reader.IsDBNull(6) ? null : reader.GetString(6)
        };
        reader.Close();

        // Fetch attributes
        await using var attrCmd = new NpgsqlCommand(
            "SELECT key, value FROM node_attributes WHERE node_id = @id", conn);
        attrCmd.Parameters.AddWithValue("id", nodeId);

        await using var attrReader = await attrCmd.ExecuteReaderAsync();
        while (await attrReader.ReadAsync())
        {
            info.Attributes[attrReader.GetString(0)] = attrReader.GetString(1);
        }

        return info;
    }

    public async ValueTask DisposeAsync()
    {
        if (_cts != null)
        {
            await _cts.CancelAsync();
            if (_listenerTask != null)
            {
                try { await _listenerTask; } catch (OperationCanceledException) { }
            }
            _cts.Dispose();
        }
        if (_conn != null)
            await _conn.DisposeAsync();
    }
}

// --- Types ---

internal class NodeInfo
{
    public Guid Id { get; set; }
    public string NodeType { get; set; } = "";
    public string? Name { get; set; }
    public string? Value { get; set; }
    public string? ParentType { get; set; }
    public string? ParentName { get; set; }
    public string? ModifiedBy { get; set; }
    public Dictionary<string, string> Attributes { get; set; } = new();
}

internal class TriggerConfig
{
    public List<TriggerRule> Triggers { get; set; } = new();
}

internal class TriggerRule
{
    public string Name { get; set; } = "";
    public bool Enabled { get; set; } = true;
    public List<string> NodeTypes { get; set; } = new();
    public Dictionary<string, string>? Conditions { get; set; }
    public string Agent { get; set; } = "claude";
    public string PromptTemplate { get; set; } = "";
}
