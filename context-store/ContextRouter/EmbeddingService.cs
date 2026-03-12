// CodeStoragePoc - Embedding Service
//
// Ollama embedding helper. Computes 768-dim embeddings via nomic-embed-text
// for storage (search_document: prefix) and search (search_query: prefix).
//
// Same pattern as Python _embed_text/_embed_query in agent-context-mcp.
// 2s timeout — returns null on failure so callers can degrade gracefully.
//
// Depends on: Ollama HTTP API (localhost:11434)
// Used by:    ChatService (embed turns), ExtractionService (embed extracted items),
//             ContextAssembler (embed queries for semantic search)

using System.Net.Http.Json;
using System.Text.Json;

namespace CodeStoragePoc.ContextRouter;

public class EmbeddingService
{
    private readonly HttpClient _httpClient;
    private readonly string _baseUrl;
    private readonly string _model;
    private const int ExpectedDimensions = 768;

    public EmbeddingService()
    {
        _baseUrl = Environment.GetEnvironmentVariable("OLLAMA_BASE_URL") ?? "http://localhost:11434";
        _model = Environment.GetEnvironmentVariable("OLLAMA_EMBED_MODEL") ?? "nomic-embed-text";
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
    }

    /// <summary>
    /// Embed text for storage. Uses "search_document:" prefix per nomic convention.
    /// Returns 768-dim float array, or null if Ollama is unavailable or errors.
    /// </summary>
    public async Task<float[]?> EmbedDocumentAsync(string text)
    {
        return await EmbedAsync($"search_document: {text}");
    }

    /// <summary>
    /// Embed text for search. Uses "search_query:" prefix per nomic convention.
    /// Returns 768-dim float array, or null if Ollama is unavailable or errors.
    /// </summary>
    public async Task<float[]?> EmbedQueryAsync(string text)
    {
        return await EmbedAsync($"search_query: {text}");
    }

    private async Task<float[]?> EmbedAsync(string prompt)
    {
        if (string.IsNullOrWhiteSpace(prompt))
            return null;

        try
        {
            var request = new { model = _model, prompt };
            var response = await _httpClient.PostAsJsonAsync($"{_baseUrl}/api/embeddings", request);
            response.EnsureSuccessStatusCode();

            var json = await response.Content.ReadFromJsonAsync<JsonElement>();
            if (!json.TryGetProperty("embedding", out var embeddingArray))
            {
                Console.WriteLine("[WARN] Ollama response missing 'embedding' field");
                return null;
            }

            var embedding = new float[embeddingArray.GetArrayLength()];
            int i = 0;
            foreach (var val in embeddingArray.EnumerateArray())
            {
                embedding[i++] = val.GetSingle();
            }

            if (embedding.Length != ExpectedDimensions)
            {
                Console.WriteLine($"[WARN] Embedding dimension mismatch: got {embedding.Length}, expected {ExpectedDimensions}");
                return null;
            }

            return embedding;
        }
        catch (TaskCanceledException)
        {
            // HttpClient timeout (2s)
            Console.WriteLine("[WARN] Ollama embedding timed out after 2s");
            return null;
        }
        catch (HttpRequestException ex)
        {
            Console.WriteLine($"[WARN] Ollama embedding request failed: {ex.Message}");
            return null;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Ollama embedding unexpected error: {ex.Message}");
            return null;
        }
    }
}
