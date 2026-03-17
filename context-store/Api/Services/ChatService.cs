// CodeStoragePoc.Api - Chat Service
//
// Orchestrates the full conversation loop:
// resolve subject → clarify or assemble state → build prompt → stream via CLI → store turn → extract ideas
//
// All model providers use CLI-based routing (subscription billing, no API keys):
//   @sonnet, @opus, @haiku, @claude  → claude -p - --output-format text
//   @gemini, @flash, @pro            → gemini -p -
//   @codex, @gpt                     → codex exec --
//   @ollama, @qwen                   → Local Ollama HTTP (port 11434)
//   (no mention)                     → defaults to Claude Sonnet
//
// Depends on: NodeRepository, ContextAssembler, PromptBuilder, InterpreterService,
//             IntentClassifier (fallback), ExtractionService, SkillLoader
// Used by:    Api/Program.cs endpoints

using System.Diagnostics;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using CodeStoragePoc.ContextRouter;
using CodeStoragePoc.DbLayer;
using Npgsql;

namespace CodeStoragePoc.Api.Services;

public record ModelRoute(string Provider, string ModelId, string DisplayName);

public class ChatService
{
    private readonly NodeRepository _repo;
    private readonly ContextAssembler _assembler;
    private readonly PromptBuilder _promptBuilder;
    private readonly IntentClassifier _classifier;
    private readonly InterpreterService _interpreter;
    private readonly SkillLoader _skillLoader;
    private readonly ExtractionService _extractionService;
    private readonly EmbeddingService _embeddingService;
    private readonly PermissionService? _permissionService;
    private readonly PendingActionService? _pendingActionService;
    private readonly CodeStoragePoc.GraphLayer.AgeLayer _ageLayer;
    private readonly string _connStr;
    private readonly HttpClient _httpClient;

    private static readonly Guid ProjectId = Guid.TryParse(
        Environment.GetEnvironmentVariable("CODESTORAGE_PROJECT_ID"), out var pid)
        ? pid : new("8196b44e-6299-45a0-a5b0-bbd111f2990b");
    private const string DefaultUser = "jruss";
    private static readonly string OllamaBaseUrl =
        Environment.GetEnvironmentVariable("OLLAMA_BASE_URL") ?? "http://localhost:11434";

    // @mention → model routing table
    // CLI providers: claude, gemini, codex (subscription billing via CLI tools)
    // HTTP providers: ollama (local inference)
    private static readonly Dictionary<string, ModelRoute> ModelRoutes = new(StringComparer.OrdinalIgnoreCase)
    {
        // Claude CLI — claude -p - --output-format text
        ["@sonnet"]  = new("claude", "sonnet", "sonnet"),
        ["@opus"]    = new("claude", "opus", "opus"),
        ["@haiku"]   = new("claude", "haiku", "haiku"),
        ["@claude"]  = new("claude", "sonnet", "claude"),
        // Gemini CLI — gemini -p -
        ["@gemini"]  = new("gemini", "gemini-2.5-flash", "gemini"),
        ["@flash"]   = new("gemini", "gemini-2.5-flash", "flash"),
        ["@pro"]     = new("gemini", "gemini-2.5-pro", "pro"),
        // Codex CLI — codex exec --
        ["@codex"]   = new("codex", "gpt-4.1", "codex"),
        ["@gpt"]     = new("codex", "gpt-4.1", "gpt"),
        // Ollama HTTP — local inference
        ["@ollama"]  = new("ollama", "qwen3.5:4b", "ollama"),
        ["@qwen"]    = new("ollama", "qwen3.5:4b", "qwen"),
    };

    // Regex to match @mention at start of message or anywhere
    private static readonly Regex MentionPattern = new(
        @"@(sonnet|opus|haiku|claude|gemini|flash|pro|codex|gpt|ollama|qwen)\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    public ChatService(
        NodeRepository repo,
        ContextAssembler assembler,
        PromptBuilder promptBuilder,
        IntentClassifier classifier,
        InterpreterService interpreter,
        SkillLoader skillLoader,
        ExtractionService extractionService,
        string connStr,
        EmbeddingService embeddingService,
        CodeStoragePoc.GraphLayer.AgeLayer ageLayer,
        PermissionService? permissionService = null,
        PendingActionService? pendingActionService = null)
    {
        _repo = repo;
        _assembler = assembler;
        _promptBuilder = promptBuilder;
        _classifier = classifier;
        _interpreter = interpreter;
        _skillLoader = skillLoader;
        _extractionService = extractionService;
        _embeddingService = embeddingService;
        _ageLayer = ageLayer;
        _permissionService = permissionService;
        _pendingActionService = pendingActionService;
        _connStr = connStr;
        _httpClient = new HttpClient { Timeout = TimeSpan.FromMinutes(2) };
    }

    /// <summary>Preview pipeline without calling the model — dry-run parse, classify, context assembly.</summary>
    public async Task<object> PreviewAsync(string message, Guid? conversationId)
    {
        var (route, cleanedMessage) = ParseMention(message);
        var intentResult = _classifier.Classify(cleanedMessage);

        // Context assembly needs a conversation — use existing or report empty
        var contextNodes = new List<object>();
        int tokenEstimate = 0;
        string promptPreview = "";

        if (conversationId.HasValue)
        {
            var context = await _assembler.Assemble(intentResult, cleanedMessage, conversationId.Value);
            var prompt = _promptBuilder.Build(context);
            tokenEstimate = (prompt.SystemPrompt.Length + prompt.UserPrompt.Length) / 4; // rough char/4 estimate
            promptPreview = prompt.SystemPrompt.Length > 300
                ? prompt.SystemPrompt[..300] + "..."
                : prompt.SystemPrompt;

            // Expose context node summaries from all context lists
            var allNodes = context.RelevantIdeas
                .Concat(context.ConnectedNodes)
                .Concat(context.OpenThreads)
                .Concat(context.CodeContext);
            foreach (var node in allNodes)
            {
                contextNodes.Add(new
                {
                    nodeType = node.NodeType,
                    name = node.Name,
                    score = node.SimilarityScore
                });
            }
        }

        return new
        {
            parse = new
            {
                originalMessage = message,
                mention = $"@{route.DisplayName}",
                cleanedMessage,
                model = new { provider = route.Provider, modelId = route.ModelId, display = route.DisplayName }
            },
            intent = new
            {
                intent = intentResult.Intent.ToString(),
                confidence = intentResult.Confidence,
                pattern = intentResult.MatchedPattern
            },
            context = new
            {
                nodeCount = contextNodes.Count,
                nodes = contextNodes,
                tokenEstimate
            },
            promptPreview
        };
    }

    /// <summary>Parse @mention from message, return (route, cleaned message).</summary>
    private static (ModelRoute Route, string CleanedMessage) ParseMention(string message)
    {
        var match = MentionPattern.Match(message);
        if (!match.Success)
            return (ModelRoutes["@sonnet"], message); // default

        var mention = $"@{match.Groups[1].Value}";
        var route = ModelRoutes[mention];
        var cleaned = MentionPattern.Replace(message, "", 1).Trim();
        return (route, cleaned);
    }

    public async Task StreamResponse(string message, Guid? conversationId, Microsoft.AspNetCore.Http.HttpResponse http, CancellationToken ct)
    {
        var totalSw = System.Diagnostics.Stopwatch.StartNew();
        var stepSw = System.Diagnostics.Stopwatch.StartNew();

        // 0. Parse @mention
        var (route, cleanedMessage) = ParseMention(message);
        var parseMs = stepSw.ElapsedMilliseconds;

        // Send parse debug event
        await SendSseEvent(http, "debug_parse", JsonSerializer.Serialize(new
        {
            originalMessage = message,
            mention = $"@{route.DisplayName}",
            cleanedMessage,
            model = new { provider = route.Provider, modelId = route.ModelId, display = route.DisplayName },
            durationMs = parseMs
        }), ct);

        // 1. Get or create conversation
        conversationId ??= await CreateConversation();

        // 2. Create thread for this exchange + store user turn under it
        var threadTopic = cleanedMessage.Length > 80 ? cleanedMessage[..80] + "..." : cleanedMessage;
        var threadId = await CreateThread(conversationId.Value, threadTopic, route.DisplayName);
        var userTurnId = await StoreTurn(threadId, "user", message);

        // Send thread info
        await SendSseEvent(http, "thread", JsonSerializer.Serialize(new
        {
            id = threadId,
            topic = threadTopic,
            model = route.DisplayName
        }), ct);

        // 3. Resolve what the user is talking about (replaces intent classification)
        stepSw.Restart();
        await SendSseEvent(http, "phase", JsonSerializer.Serialize(new { phase = "resolving" }), ct);
        var resolved = await _interpreter.ResolveAsync(cleanedMessage, conversationId);
        var classifyMs = stepSw.ElapsedMilliseconds;

        // Send resolution debug event
        await SendSseEvent(http, "debug_interpret", JsonSerializer.Serialize(new
        {
            intent = resolved.DisplayIntent.ToString(),
            confidence = resolved.Confidence,
            reasoning = resolved.Reasoning,
            isResolved = resolved.IsResolved,
            project = new
            {
                type = resolved.Project.Type,
                name = resolved.Project.ProjectName,
                id = resolved.Project.ProjectId
            },
            entities = resolved.Entities.Select(e => new
            {
                mention = e.Mention,
                nodeId = e.NodeId,
                nodeName = e.NodeName,
                nodeType = e.NodeType,
                score = e.Score
            }),
            subjectTypes = resolved.SubjectTypes.ToArray(),
            isRegexFallback = resolved.IsRegexFallback,
            durationMs = resolved.DurationMs,
            responseGuidance = resolved.ResponseGuidance,
            clarification = resolved.ClarificationQuestion
        }), ct);

        // Store resolution as a node (backward compat: convert to InterpretedInput for storage)
        var interpreted = new InterpretedInput
        {
            Project = resolved.Project,
            Intent = resolved.DisplayIntent,
            Confidence = resolved.Confidence,
            Entities = resolved.Entities,
            CleanedMessage = cleanedMessage,
            Reasoning = resolved.Reasoning,
            DurationMs = resolved.DurationMs,
            IsRegexFallback = resolved.IsRegexFallback,
            ResponseGuidance = resolved.ResponseGuidance
        };
        var interpretNodeId = await StoreInterpretation(threadId, interpreted);

        // Send metadata SSE events (legacy format for existing frontend)
        await SendSseEvent(http, "conversation_id", JsonSerializer.Serialize(new { id = conversationId.Value }), ct);
        await SendSseEvent(http, "intent", JsonSerializer.Serialize(new
        {
            intent = resolved.DisplayIntent.ToString(),
            confidence = resolved.Confidence.ToString("F2"),
            pattern = resolved.Reasoning
        }), ct);
        await SendSseEvent(http, "model", JsonSerializer.Serialize(new
        {
            provider = route.Provider,
            model = route.ModelId,
            display = route.DisplayName
        }), ct);

        // 3b. If resolution failed, return clarification question instead of calling model
        if (!resolved.IsResolved)
        {
            var clarificationText = resolved.ClarificationQuestion
                ?? "I'm not sure what you're referring to. Could you be more specific?";
            if (resolved.CandidateMatches.Count > 0)
            {
                clarificationText += "\n\nDid you mean one of these?";
                foreach (var c in resolved.CandidateMatches)
                {
                    var nameStr = c.NodeName ?? c.Mention;
                    var typeStr = c.NodeType != null ? $" ({c.NodeType})" : "";
                    clarificationText += $"\n- {nameStr}{typeStr}";
                }
            }

            // Emit as tokens (typewriter effect)
            await EmitTypewriter(clarificationText, http, ct);

            // Store as model turn
            await StoreTurn(threadId, route.DisplayName, clarificationText);

            await SendSseEvent(http, "debug_timing", JsonSerializer.Serialize(new
            {
                parseMs,
                classifyMs,
                contextMs = 0,
                generateMs = 0,
                extractMs = 0,
                totalMs = totalSw.ElapsedMilliseconds
            }), ct);
            await SendSseEvent(http, "done", "{}", ct);
            return;
        }

        // 3c. Handle direct actions (park/resume) without calling a model
        if (resolved.IsDirectAction)
        {
            var actionText = await HandleDirectAction(resolved, conversationId.Value);
            await EmitTypewriter(actionText, http, ct);
            await StoreTurn(threadId, route.DisplayName, actionText);

            await SendSseEvent(http, "debug_timing", JsonSerializer.Serialize(new
            {
                parseMs,
                classifyMs,
                contextMs = 0,
                generateMs = 0,
                extractMs = 0,
                totalMs = totalSw.ElapsedMilliseconds
            }), ct);
            await SendSseEvent(http, "done", "{}", ct);
            return;
        }

        // 4. Assemble state around the resolved subject
        stepSw.Restart();
        var subjectState = await _assembler.AssembleFromSubject(resolved, conversationId.Value);
        var contextMs = stepSw.ElapsedMilliseconds;

        // Send context debug event
        var contextNodes = subjectState.ConnectedNodes
            .Concat(subjectState.RelatedNodes)
            .Concat(subjectState.OpenItems)
            .Select(n => new
            {
                nodeType = n.NodeType,
                name = n.Name,
                score = n.SimilarityScore
            }).ToList();

        await SendSseEvent(http, "debug_context", JsonSerializer.Serialize(new
        {
            nodeCount = contextNodes.Count + subjectState.ResolvedNodeStates.Count,
            nodes = contextNodes,
            resolvedNodes = subjectState.ResolvedNodeStates.Select(n => new
            {
                nodeType = n.NodeType,
                name = n.Name,
                childCount = n.ChildCount
            }),
            coverage = subjectState.Coverage != null ? new { subjectState.Coverage.Embedded, subjectState.Coverage.Total, subjectState.Coverage.Pct } : null,
            durationMs = contextMs
        }), ct);

        // 5. Enrich state with identity, permissions, and available skills
        var permLevel = PermissionLevel.Assist;
        if (_permissionService != null && conversationId.HasValue)
            permLevel = await _permissionService.GetEffectiveLevel(conversationId, route.DisplayName);
        var skillNames = _skillLoader.GetSkillNames();
        subjectState = subjectState with
        {
            RespondingModel = route.DisplayName,
            PermissionLabel = permLevel.ToString(),
            AvailableSkills = skillNames
        };

        // Build prompt from subject state (new path: contracts + state, no chat history)
        var prompt = _promptBuilder.BuildFromSubjectState(subjectState, cleanedMessage);
        var tokenEstimate = (prompt.SystemPrompt.Length + prompt.UserPrompt.Length) / 4;

        await SendSseEvent(http, "debug_prompt", JsonSerializer.Serialize(new
        {
            systemPromptLength = prompt.SystemPrompt.Length,
            userPromptLength = prompt.UserPrompt.Length,
            tokenEstimate
        }), ct);

        // 6. Stream response from selected provider
        stepSw.Restart();
        var fullResponse = new StringBuilder();

        if (route.Provider == "ollama")
            await StreamOllama(route, prompt, fullResponse, http, ct);
        else
            await StreamCli(route, prompt, fullResponse, http, ct);

        var generateMs = stepSw.ElapsedMilliseconds;

        // 7. Store model turn (under the same thread)
        var modelTurnId = await StoreTurn(threadId, route.DisplayName, fullResponse.ToString());

        // 8. Extract ideas (synchronous for v1)
        await SendSseEvent(http, "phase", JsonSerializer.Serialize(new { phase = "extracting" }), ct);
        stepSw.Restart();
        var extracted = await _extractionService.ExtractFromResponse(
            fullResponse.ToString(), threadId, modelTurnId);
        var extractMs = stepSw.ElapsedMilliseconds;

        // 9. Send extraction results + timing + done
        await SendSseEvent(http, "extraction", JsonSerializer.Serialize(extracted,
            new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }), ct);
        await SendSseEvent(http, "debug_timing", JsonSerializer.Serialize(new
        {
            parseMs,
            classifyMs,
            contextMs,
            generateMs,
            extractMs,
            totalMs = totalSw.ElapsedMilliseconds
        }), ct);
        await SendSseEvent(http, "done", "{}", ct);
    }

    /// <summary>Stream response from a CLI provider (claude, gemini, codex). Spawns the CLI,
    /// pipes the prompt to stdin, reads stdout in chunks and emits SSE token events.</summary>
    private async Task StreamCli(ModelRoute route, AssembledPrompt prompt, StringBuilder fullResponse,
        Microsoft.AspNetCore.Http.HttpResponse http, CancellationToken ct)
    {
        // Build CLI command + args per provider
        string executable;
        var args = new List<string>();

        switch (route.Provider)
        {
            case "claude":
                executable = "claude";
                args.AddRange(["--print", "-", "--output-format", "text"]);
                args.AddRange(["--model", route.ModelId]);
                break;
            case "gemini":
                executable = "gemini";
                args.AddRange(["-p", "-"]);
                args.AddRange(["-m", route.ModelId]);
                break;
            case "codex":
                executable = "codex";
                args.AddRange(["exec", "--"]);
                args.AddRange(["--model", route.ModelId]);
                break;
            default:
                await SendSseEvent(http, "token", JsonSerializer.Serialize(new { text = $"Unknown CLI provider: {route.Provider}" }), ct);
                return;
        }

        // Combine system + user prompts (CLIs take a single prompt via stdin)
        var combinedPrompt = $"<system>\n{prompt.SystemPrompt}\n</system>\n\n{prompt.UserPrompt}";

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

        Process? process;
        try
        {
            process = Process.Start(psi);
        }
        catch (Exception ex)
        {
            await SendSseEvent(http, "token", JsonSerializer.Serialize(new
            {
                text = $"Failed to start {executable} CLI (resolved: {resolvedExe}): {ex.Message}\n\nMake sure the CLI is installed and on your PATH."
            }), ct);
            return;
        }

        if (process == null)
        {
            await SendSseEvent(http, "token", JsonSerializer.Serialize(new { text = $"Failed to start {executable} CLI" }), ct);
            return;
        }

        using (process)
        {
            // Write prompt to stdin, then close to signal EOF
            await process.StandardInput.WriteAsync(combinedPrompt.AsMemory(), ct);
            process.StandardInput.Close();

            // Read stderr in background to prevent deadlock
            var stderrTask = process.StandardError.ReadToEndAsync(ct);

            // Read full response (CLIs return all output at once, not streamed)
            var rawOutput = await process.StandardOutput.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);

            if (process.ExitCode != 0)
            {
                var stderr = await stderrTask;
                if (!string.IsNullOrWhiteSpace(stderr))
                {
                    rawOutput += $"\n\n[{executable} error (exit {process.ExitCode})]: {stderr.Trim()}";
                }
            }

            fullResponse.Append(rawOutput);

            // Typewriter effect — emit word-by-word with small delays
            await EmitTypewriter(rawOutput, http, ct);
        }
    }

    /// <summary>Emit text word-by-word as SSE token events with small delays to simulate streaming.</summary>
    private static async Task EmitTypewriter(string text, Microsoft.AspNetCore.Http.HttpResponse http, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(text)) return;

        // Split on word boundaries, preserving whitespace with each word
        // e.g. "Hello world\n\nNew paragraph" → ["Hello ", "world\n\n", "New ", "paragraph"]
        var words = Regex.Matches(text, @"\S+\s*");

        foreach (Match word in words)
        {
            ct.ThrowIfCancellationRequested();
            await SendSseEvent(http, "token", JsonSerializer.Serialize(new { text = word.Value }), ct);
            await Task.Delay(20, ct); // 20ms per word ≈ natural reading speed
        }
    }

    private async Task StreamOllama(ModelRoute route, AssembledPrompt prompt, StringBuilder fullResponse,
        Microsoft.AspNetCore.Http.HttpResponse http, CancellationToken ct)
    {
        var requestBody = new
        {
            model = route.ModelId,
            messages = new[]
            {
                new { role = "system", content = prompt.SystemPrompt },
                new { role = "user", content = prompt.UserPrompt }
            },
            stream = true,
            think = false
        };

        var request = new HttpRequestMessage(HttpMethod.Post, $"{OllamaBaseUrl}/api/chat")
        {
            Content = JsonContent.Create(requestBody)
        };

        using var response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        response.EnsureSuccessStatusCode();

        using var responseStream = await response.Content.ReadAsStreamAsync(ct);
        using var reader = new StreamReader(responseStream);

        while (!reader.EndOfStream)
        {
            var line = await reader.ReadLineAsync(ct);
            if (string.IsNullOrEmpty(line)) continue;

            try
            {
                using var doc = JsonDocument.Parse(line);
                var msg = doc.RootElement.GetProperty("message");
                var content = msg.GetProperty("content").GetString();
                if (!string.IsNullOrEmpty(content))
                {
                    fullResponse.Append(content);
                    await SendSseEvent(http, "token", JsonSerializer.Serialize(new { text = content }), ct);
                }
            }
            catch (JsonException)
            {
                // Skip malformed lines
            }
        }
    }

    public async Task<object> GetAvailableModels()
    {
        var models = ModelRoutes.Select(kv => new
        {
            mention = kv.Key,
            provider = kv.Value.Provider,
            model = kv.Value.ModelId,
            display = kv.Value.DisplayName
        });
        return new { models };
    }

    public async Task<Guid> CreateConversation()
    {
        var id = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'conversation', @name, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", id);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("name", $"Conversation {DateTime.UtcNow:yyyy-MM-dd HH:mm}");
        cmd.Parameters.AddWithValue("user", DefaultUser);
        await cmd.ExecuteNonQueryAsync();

        return id;
    }

    private async Task<Guid> CreateThread(Guid conversationId, string topic, string model)
    {
        var threadId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var countCmd = conn.CreateCommand();
        countCmd.CommandText = "SELECT COUNT(*) FROM nodes WHERE parent_id = @convId AND node_type = 'thread'";
        countCmd.Parameters.AddWithValue("convId", conversationId);
        var count = (long)(await countCmd.ExecuteScalarAsync())!;

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'thread', @name, NULL, @parentId, @order, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", threadId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("name", topic);
        cmd.Parameters.AddWithValue("parentId", conversationId);
        cmd.Parameters.AddWithValue("order", (int)count * 100);
        cmd.Parameters.AddWithValue("user", DefaultUser);
        await cmd.ExecuteNonQueryAsync();

        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value) VALUES (@id, 'model', @model), (@id, 'status', 'active')";
        attrCmd.Parameters.AddWithValue("id", threadId);
        attrCmd.Parameters.AddWithValue("model", model);
        await attrCmd.ExecuteNonQueryAsync();

        // Sync to AGE so graph traversal can find this thread immediately
        await _ageLayer.SyncVertex(threadId, "thread", topic);

        return threadId;
    }

    /// <summary>Public wrapper for brain API — stores a turn under a given parent node.</summary>
    public Task<Guid> StoreTurnPublic(Guid conversationId, Guid? threadId, string speaker, string content)
        => StoreTurn(threadId ?? conversationId, speaker, content);

    private async Task<Guid> StoreTurn(Guid parentId, string speaker, string content)
    {
        var turnId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var countCmd = conn.CreateCommand();
        countCmd.CommandText = "SELECT COUNT(*) FROM nodes WHERE parent_id = @parentId AND node_type = 'turn'";
        countCmd.Parameters.AddWithValue("parentId", parentId);
        var count = (long)(await countCmd.ExecuteScalarAsync())!;

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'turn', @name, @value, @parentId, @order, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", turnId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("name", speaker);
        cmd.Parameters.AddWithValue("value", content);
        cmd.Parameters.AddWithValue("parentId", parentId);
        cmd.Parameters.AddWithValue("order", (int)count);
        cmd.Parameters.AddWithValue("user", DefaultUser);
        await cmd.ExecuteNonQueryAsync();

        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value) VALUES (@nodeId, 'speaker', @speaker)";
        attrCmd.Parameters.AddWithValue("nodeId", turnId);
        attrCmd.Parameters.AddWithValue("speaker", speaker);
        await attrCmd.ExecuteNonQueryAsync();

        // Sync to AGE so graph traversal can find this turn immediately
        await _ageLayer.SyncVertex(turnId, "turn", speaker);

        // Fire-and-forget embedding — don't block the SSE stream
        _ = Task.Run(async () =>
        {
            try
            {
                var textToEmbed = $"{speaker}: {content}";
                var embedding = await _embeddingService.EmbedDocumentAsync(textToEmbed);
                if (embedding != null)
                    await _repo.SetEmbedding(turnId, embedding);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[WARN] Background embedding failed for turn {turnId}: {ex.Message}");
            }
        });

        return turnId;
    }

    public async Task<List<object>> ListConversations()
    {
        var results = new List<object>();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT id, name, created_at, modified_by,
                   (SELECT COUNT(*) FROM nodes WHERE node_type = 'turn'
                    AND (parent_id = c.id OR parent_id IN (SELECT id FROM nodes WHERE parent_id = c.id AND node_type = 'thread'))) as turn_count
            FROM nodes c
            WHERE c.node_type = 'conversation' AND c.project_id = @projectId
            ORDER BY c.created_at DESC";
        cmd.Parameters.AddWithValue("projectId", ProjectId);

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            results.Add(new
            {
                id = reader.GetGuid(0),
                name = reader.GetString(1),
                createdAt = reader.GetDateTime(2),
                user = reader.IsDBNull(3) ? null : reader.GetString(3),
                turnCount = reader.GetInt64(4)
            });
        }
        return results;
    }

    public async Task<object> GetConversation(Guid conversationId)
    {
        var turns = new List<object>();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var convCmd = conn.CreateCommand();
        convCmd.CommandText = "SELECT name, created_at FROM nodes WHERE id = @id";
        convCmd.Parameters.AddWithValue("id", conversationId);
        string? convName = null;
        DateTime? convCreatedAt = null;
        await using (var reader = await convCmd.ExecuteReaderAsync())
        {
            if (await reader.ReadAsync())
            {
                convName = reader.GetString(0);
                convCreatedAt = reader.GetDateTime(1);
            }
        }

        // Fetch turns from both direct children (legacy) and through threads (new)
        await using var turnCmd = conn.CreateCommand();
        turnCmd.CommandText = @"
            SELECT t.id, t.name, t.value, t.created_at
            FROM nodes t
            WHERE t.node_type = 'turn'
              AND (t.parent_id = @convId
                   OR t.parent_id IN (SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'))
            ORDER BY t.created_at ASC";
        turnCmd.Parameters.AddWithValue("convId", conversationId);

        await using var turnReader = await turnCmd.ExecuteReaderAsync();
        while (await turnReader.ReadAsync())
        {
            turns.Add(new
            {
                id = turnReader.GetGuid(0),
                speaker = turnReader.GetString(1),
                content = turnReader.IsDBNull(2) ? "" : turnReader.GetString(2),
                createdAt = turnReader.GetDateTime(3)
            });
        }

        return new { id = conversationId, name = convName, createdAt = convCreatedAt, turns };
    }

    public async Task<object> GetThreads(Guid conversationId)
    {
        var ideas = new List<object>();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            SELECT n.id, n.node_type, n.name, n.value,
                   COALESCE(a.value, 'mentioned') as status
            FROM nodes n
            LEFT JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.node_type IN ('idea', 'question', 'decision', 'action_item')
              AND (n.parent_id = @convId
                   OR n.parent_id IN (SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'))
            ORDER BY n.created_at DESC";
        cmd.Parameters.AddWithValue("convId", conversationId);

        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            ideas.Add(new
            {
                id = reader.GetGuid(0),
                nodeType = reader.GetString(1),
                name = reader.IsDBNull(2) ? null : reader.GetString(2),
                value = reader.IsDBNull(3) ? null : reader.GetString(3),
                status = reader.GetString(4)
            });
        }

        return new { conversationId, items = ideas };
    }

    public async Task<object> GetStats(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        // Count turns/ideas through threads (new) and direct children (legacy)
        cmd.CommandText = @"
            WITH RECURSIVE descendants AS (
                SELECT id FROM nodes WHERE parent_id = @id
                UNION ALL
                SELECT n.id FROM nodes n JOIN descendants d ON n.parent_id = d.id
            )
            SELECT
                (SELECT COUNT(*) FROM nodes WHERE id IN (SELECT id FROM descendants) AND node_type = 'turn') as turns,
                (SELECT COUNT(*) FROM nodes WHERE id IN (SELECT id FROM descendants) AND node_type = 'idea') as ideas,
                (SELECT COUNT(*) FROM nodes WHERE id IN (SELECT id FROM descendants) AND node_type = 'question') as questions,
                (SELECT COUNT(*) FROM nodes n
                 JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status' AND a.value = 'parked'
                 WHERE n.id IN (SELECT id FROM descendants) AND n.node_type = 'idea') as parked";
        cmd.Parameters.AddWithValue("id", conversationId);

        await using var reader = await cmd.ExecuteReaderAsync();
        await reader.ReadAsync();

        return new
        {
            conversationId,
            turnCount = reader.GetInt64(0),
            ideaCount = reader.GetInt64(1),
            questionCount = reader.GetInt64(2),
            parkedCount = reader.GetInt64(3)
        };
    }

    public async Task StoreSystemTurn(Guid conversationId, string text, string level)
    {
        var turnId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var countCmd = conn.CreateCommand();
        countCmd.CommandText = "SELECT COUNT(*) FROM nodes WHERE parent_id = @convId AND node_type = 'turn'";
        countCmd.Parameters.AddWithValue("convId", conversationId);
        var count = (long)(await countCmd.ExecuteScalarAsync())!;

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'turn', 'system', @value, @parentId, @order, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", turnId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("value", text);
        cmd.Parameters.AddWithValue("parentId", conversationId);
        cmd.Parameters.AddWithValue("order", (int)count);
        cmd.Parameters.AddWithValue("user", "system");
        await cmd.ExecuteNonQueryAsync();

        await using var attrCmd = conn.CreateCommand();
        attrCmd.CommandText = @"
            INSERT INTO node_attributes (node_id, key, value) VALUES (@nodeId, 'speaker', 'system'), (@nodeId, 'level', @level)";
        attrCmd.Parameters.AddWithValue("nodeId", turnId);
        attrCmd.Parameters.AddWithValue("level", level);
        await attrCmd.ExecuteNonQueryAsync();
    }

    public async Task ParkIdea(Guid nodeId) => await SetIdeaStatus(nodeId, "parked");
    public async Task ResumeIdea(Guid nodeId) => await SetIdeaStatus(nodeId, "explored");

    /// <summary>Handle park/resume direct actions without calling a model.</summary>
    private async Task<string> HandleDirectAction(ResolvedSubject resolved, Guid conversationId)
    {
        var entityId = resolved.Entities.FirstOrDefault(e => e.NodeId.HasValue)?.NodeId;

        if (resolved.DirectActionType == "park")
        {
            if (entityId.HasValue)
            {
                await ParkIdea(entityId.Value);
                var entityName = resolved.Entities.First(e => e.NodeId == entityId).NodeName ?? resolved.Entities.First(e => e.NodeId == entityId).Mention;
                return $"Parked \"{entityName}\". It'll be tracked and can be resumed later.";
            }
            return "I'd park that, but I'm not sure which idea you mean. Can you be more specific?";
        }

        if (resolved.DirectActionType == "resume")
        {
            if (entityId.HasValue)
            {
                await ResumeIdea(entityId.Value);
                var entityName = resolved.Entities.First(e => e.NodeId == entityId).NodeName ?? resolved.Entities.First(e => e.NodeId == entityId).Mention;
                return $"Resumed \"{entityName}\". Where do you want to pick up?";
            }
            return "Which idea do you want to resume? I have a few parked ones.";
        }

        return "I'm not sure what action to take. Can you clarify?";
    }

    /// <summary>Store the Interpreter's output as a node under the thread. Makes interpretations
    /// queryable, versionable, and visible in the workspace view.</summary>
    private async Task<Guid> StoreInterpretation(Guid threadId, InterpretedInput interpreted)
    {
        var nodeId = Guid.NewGuid();
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Summary for the node name — e.g. "ideation (95%) → WeatherDashboard [new]"
        var nameParts = new List<string> { interpreted.Intent.ToString().ToLower() };
        nameParts.Add($"({(interpreted.Confidence * 100):F0}%)");
        if (interpreted.Project.ProjectName != null)
            nameParts.Add($"→ {interpreted.Project.ProjectName}");
        nameParts.Add($"[{interpreted.Project.Type}]");
        var name = string.Join(" ", nameParts);

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = @"
            INSERT INTO nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by, created_at, modified_at)
            VALUES (@id, @projectId, 'interpretation', @name, @value, @parentId, 0, @user, NOW(), NOW())";
        cmd.Parameters.AddWithValue("id", nodeId);
        cmd.Parameters.AddWithValue("projectId", ProjectId);
        cmd.Parameters.AddWithValue("name", name);
        cmd.Parameters.AddWithValue("value", interpreted.Reasoning ?? (object)DBNull.Value);
        cmd.Parameters.AddWithValue("parentId", threadId);
        cmd.Parameters.AddWithValue("user", "interpreter");
        await cmd.ExecuteNonQueryAsync();

        // Store structured attributes
        var attrs = new Dictionary<string, string>
        {
            ["intent"] = interpreted.Intent.ToString(),
            ["confidence"] = interpreted.Confidence.ToString("F2"),
            ["project_type"] = interpreted.Project.Type,
            ["is_regex_fallback"] = interpreted.IsRegexFallback.ToString(),
            ["duration_ms"] = interpreted.DurationMs.ToString()
        };
        if (interpreted.Project.ProjectName != null)
            attrs["project_name"] = interpreted.Project.ProjectName;
        if (interpreted.Project.ProjectId.HasValue)
            attrs["project_id"] = interpreted.Project.ProjectId.Value.ToString();
        if (interpreted.Reasoning != null)
            attrs["reasoning"] = interpreted.Reasoning;
        if (interpreted.ResponseGuidance != null)
            attrs["response_guidance"] = JsonSerializer.Serialize(interpreted.ResponseGuidance);

        // Store entity references as JSON attribute (compact)
        if (interpreted.Entities.Count > 0)
        {
            var entitiesJson = JsonSerializer.Serialize(interpreted.Entities.Select(e => new
            {
                mention = e.Mention,
                nodeId = e.NodeId,
                nodeName = e.NodeName,
                nodeType = e.NodeType
            }));
            attrs["entities"] = entitiesJson;
        }

        if (attrs.Count > 0)
        {
            var sb = new StringBuilder("INSERT INTO node_attributes (node_id, key, value) VALUES ");
            var i = 0;
            foreach (var kv in attrs)
            {
                if (i > 0) sb.Append(", ");
                sb.Append($"(@nodeId, @k{i}, @v{i})");
                i++;
            }

            await using var attrCmd = conn.CreateCommand();
            attrCmd.CommandText = sb.ToString();
            attrCmd.Parameters.AddWithValue("nodeId", nodeId);
            i = 0;
            foreach (var kv in attrs)
            {
                attrCmd.Parameters.AddWithValue($"k{i}", kv.Key);
                attrCmd.Parameters.AddWithValue($"v{i}", kv.Value);
                i++;
            }
            await attrCmd.ExecuteNonQueryAsync();
        }

        return nodeId;
    }

    private async Task SetIdeaStatus(Guid nodeId, string status)
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

    /// <summary>Check permission for a tool call during streaming. Returns the tool result string
    /// if allowed or denied outright. Returns null if the action was pended (needs user approval)
    /// and emits a permission_request SSE event.</summary>
    public async Task<(PermissionResult Result, Guid? ActionId)> CheckToolPermission(
        Guid? conversationId, Guid threadId, string model, string skillName,
        string description, string paramsJson,
        Microsoft.AspNetCore.Http.HttpResponse? http, CancellationToken ct)
    {
        if (_permissionService == null)
            return (PermissionResult.Allowed, null);

        var result = await _permissionService.CheckPermission(conversationId, model, skillName);

        if (result == PermissionResult.Allowed)
            return (result, null);

        if (result == PermissionResult.NeedsApproval && _pendingActionService != null && conversationId.HasValue)
        {
            var actionId = await _pendingActionService.CreatePendingAction(
                conversationId.Value, threadId, model, skillName, description, paramsJson);

            // Emit SSE event so frontend can show approval card
            if (http != null)
            {
                await SendSseEvent(http, "permission_request", JsonSerializer.Serialize(new
                {
                    actionId,
                    model,
                    skill = skillName,
                    description,
                    paramsJson
                }), ct);
            }

            return (result, actionId);
        }

        return (result, null);
    }

    private static async Task SendSseEvent(Microsoft.AspNetCore.Http.HttpResponse http, string eventType, string data, CancellationToken ct)
    {
        await http.WriteAsync($"event: {eventType}\ndata: {data}\n\n", ct);
        await http.Body.FlushAsync(ct);
    }
}
