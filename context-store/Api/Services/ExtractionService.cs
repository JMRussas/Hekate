// CodeStoragePoc.Api - Extraction Service
//
// Extracts ideas, questions, and decisions from Claude's responses.
// Uses Claude CLI for a structured extraction prompt (subscription billing).
// Extracted items become nodes under the conversation.
//
// Depends on: Claude CLI (claude -p -), NodeRepository, EmbeddingService, AgeLayer, Npgsql
// Used by:    ChatService

using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using CodeStoragePoc.ContextRouter;
using CodeStoragePoc.DbLayer;
using CodeStoragePoc.GraphLayer;
using Npgsql;

namespace CodeStoragePoc.Api.Services;

public record ExtractedItem(string Type, string Name, string Description);
public record ExtractionResult(List<ExtractedItem> Items, int Count);

public class ExtractionService
{
    private readonly NodeRepository _repo;
    private readonly string _connStr;
    private readonly EmbeddingService _embeddingService;
    private readonly CodeStoragePoc.GraphLayer.AgeLayer _ageLayer;
    private readonly HttpClient _httpClient = new() { Timeout = TimeSpan.FromSeconds(120) };

    private static readonly Guid ProjectId = Guid.TryParse(
        Environment.GetEnvironmentVariable("CODESTORAGE_PROJECT_ID"), out var pid)
        ? pid : new("8196b44e-6299-45a0-a5b0-bbd111f2990b");

    private static readonly string OllamaBaseUrl =
        Environment.GetEnvironmentVariable("OLLAMA_BASE_URL") ?? "http://localhost:11434";

    private static readonly string ExtractionModel =
        Environment.GetEnvironmentVariable("EXTRACTION_MODEL") ?? "qwen3.5:4b";

    /// <summary>Cosine distance threshold for auto-creating RELATES_TO edges between similar ideas.</summary>
    private const double RelatesToThreshold = 0.3;

    public ExtractionService(NodeRepository repo, string connStr, EmbeddingService embeddingService, CodeStoragePoc.GraphLayer.AgeLayer ageLayer)
    {
        _repo = repo;
        _connStr = connStr;
        _embeddingService = embeddingService;
        _ageLayer = ageLayer;
    }

    public async Task<ExtractionResult> ExtractFromResponse(string response, Guid conversationId, Guid turnId)
    {
        if (string.IsNullOrWhiteSpace(response) || response.Length < 50)
            return new ExtractionResult(new List<ExtractedItem>(), 0);

        var extractionPrompt = @"Extract any ideas, questions, decisions, or action items from this AI response.
Return a JSON array. Each item has: type (idea|question|decision|action_item), name (short label), description (one sentence).
If nothing worth extracting, return an empty array [].
Only extract substantive items — not greetings, acknowledgments, or meta-commentary.
Return ONLY a JSON array, no markdown fences, no preamble.";

        // Try Ollama first (fast, free, no CLI dependency), fall back to Claude CLI
        var text = await ExtractViaOllama(extractionPrompt, response)
                ?? await ExtractViaCli(extractionPrompt, response);

        if (string.IsNullOrWhiteSpace(text))
            return new ExtractionResult(new List<ExtractedItem>(), 0);

        var items = ParseExtractionJson(text);

        foreach (var item in items)
        {
            await StoreExtractedNode(item, conversationId, turnId);
        }

        return new ExtractionResult(items, items.Count);
    }

    private async Task<string?> ExtractViaOllama(string systemPrompt, string response)
    {
        try
        {
            var requestBody = new
            {
                model = ExtractionModel,
                messages = new[]
                {
                    new { role = "system", content = systemPrompt },
                    new { role = "user", content = response }
                },
                stream = false,
                think = false
            };

            var httpResponse = await _httpClient.PostAsJsonAsync($"{OllamaBaseUrl}/api/chat", requestBody);
            httpResponse.EnsureSuccessStatusCode();

            using var doc = await JsonDocument.ParseAsync(await httpResponse.Content.ReadAsStreamAsync());
            var content = doc.RootElement.GetProperty("message").GetProperty("content").GetString();
            if (!string.IsNullOrWhiteSpace(content))
            {
                Console.WriteLine($"[EXTRACT] Ollama ({ExtractionModel}) succeeded");
                return content;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXTRACT] Ollama failed, falling back to CLI: {ex.Message}");
        }

        return null;
    }

    private static async Task<string?> ExtractViaCli(string systemPrompt, string response)
    {
        try
        {
            var resolvedExe = CliResolver.Resolve("claude");

            var psi = new ProcessStartInfo
            {
                FileName = resolvedExe,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("--print");
            psi.ArgumentList.Add("-");
            psi.ArgumentList.Add("--output-format");
            psi.ArgumentList.Add("text");
            psi.ArgumentList.Add("--model");
            psi.ArgumentList.Add("haiku");

            using var process = Process.Start(psi)
                ?? throw new InvalidOperationException("Failed to start claude CLI");

            var combinedPrompt = $"<system>\n{systemPrompt}\n</system>\n\n{response}";
            await process.StandardInput.WriteAsync(combinedPrompt);
            process.StandardInput.Close();

            var text = await process.StandardOutput.ReadToEndAsync();
            await process.WaitForExitAsync();

            if (process.ExitCode != 0)
            {
                Console.WriteLine($"[EXTRACT] Claude CLI exited with code {process.ExitCode}");
                return null;
            }

            Console.WriteLine("[EXTRACT] Claude CLI succeeded (fallback)");
            return text;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXTRACT] Claude CLI failed: {ex.Message}");
            return null;
        }
    }

    private static List<ExtractedItem> ParseExtractionJson(string text)
    {
        text = text.Trim();

        // Strip markdown fences
        if (text.StartsWith("```"))
        {
            var firstNewline = text.IndexOf('\n');
            var lastFence = text.LastIndexOf("```");
            if (firstNewline > 0 && lastFence > firstNewline)
                text = text[(firstNewline + 1)..lastFence].Trim();
        }

        // Remove trailing commas before ] or }
        text = System.Text.RegularExpressions.Regex.Replace(text, @",\s*([}\]])", "$1");

        try
        {
            return JsonSerializer.Deserialize<List<ExtractedItem>>(text, new JsonSerializerOptions
            {
                PropertyNameCaseInsensitive = true
            }) ?? new List<ExtractedItem>();
        }
        catch (JsonException)
        {
            return new List<ExtractedItem>();
        }
    }

    private async Task StoreExtractedNode(ExtractedItem item, Guid conversationId, Guid turnId)
    {
        var nodeId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, created_at, modified_at)
            VALUES (@id, @projectId, @nodeType, @name, @value, @parentId, 0, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", nodeId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("nodeType", item.Type);
        cmd.Parameters.AddWithValue("name", item.Name);
        cmd.Parameters.AddWithValue("value", item.Description);
        cmd.Parameters.AddWithValue("parentId", conversationId);
        await cmd.ExecuteNonQueryAsync();

        // Store status and source turn
        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value) VALUES (@id, 'status', 'mentioned');
            INSERT INTO node_attributes (node_id, key, value) VALUES (@id, 'source_turn', @turnId)";
        attrCmd.Parameters.AddWithValue("id", nodeId);
        attrCmd.Parameters.AddWithValue("turnId", turnId.ToString());
        await attrCmd.ExecuteNonQueryAsync();

        // Sync vertices and create EXTRACTED edge (turn → extracted idea)
        await _ageLayer.SyncVertex(nodeId, item.Type, item.Name);
        // Turn should already be synced by ChatService, but MERGE is idempotent
        await _ageLayer.SyncVertex(turnId, NodeTypes.Turn, null);
        await _ageLayer.CreateEdge(turnId, nodeId, EdgeTypes.Extracted);

        // Fire-and-forget: embed + auto-wire RELATES_TO edges to similar existing ideas
        _ = Task.Run(async () =>
        {
            try
            {
                var textToEmbed = $"{item.Name} {item.Description}";
                var embedding = await _embeddingService.EmbedDocumentAsync(textToEmbed);
                if (embedding == null)
                    return;

                await _repo.SetEmbedding(nodeId, embedding);

                // Find similar existing ideas and create RELATES_TO edges
                await CreateRelatesToEdges(nodeId, embedding);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[WARN] Background embedding/edge-wiring failed for extracted node {nodeId}: {ex.Message}");
            }
        });
    }

    /// <summary>
    /// Search for existing ideas similar to the newly extracted one and create RELATES_TO edges.
    /// Uses pgvector cosine distance — threshold defined by RelatesToThreshold.
    /// </summary>
    private async Task CreateRelatesToEdges(Guid nodeId, float[] embedding)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Find similar idea-like nodes (exclude self)
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.node_type, n.name, (n.embedding <=> @vec::vector) as distance
            FROM nodes n
            WHERE n.embedding IS NOT NULL
              AND n.id != @nodeId
              AND n.node_type IN ('idea', 'question', 'decision', 'finding')
            ORDER BY n.embedding <=> @vec::vector
            LIMIT 5";
        cmd.Parameters.AddWithValue("nodeId", nodeId);
        cmd.Parameters.AddWithValue("vec", embedding);

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var distance = reader.GetDouble(3);
            if (distance > RelatesToThreshold)
                break; // Remaining results are even less similar

            var relatedId = reader.GetGuid(0);
            var relatedType = reader.GetString(1);
            var relatedName = reader.IsDBNull(2) ? null : reader.GetString(2);

            try
            {
                // Ensure the related node has an AGE vertex
                await _ageLayer.SyncVertex(relatedId, relatedType, relatedName);
                await _ageLayer.CreateEdge(nodeId, relatedId, EdgeTypes.RelatesTo);
                Console.WriteLine($"[GRAPH]    Auto-wired RELATES_TO: {nodeId} → {relatedId} (distance: {distance:F3})");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[WARN] RELATES_TO edge creation failed: {ex.Message}");
            }
        }
    }
}
