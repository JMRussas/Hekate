// CodeStoragePoc - Interpreter Service
//
// The Interpreter role: resolve what the user is talking about.
//
// Primary job: entity resolution. Given user input + conversation history +
// graph context, identify which existing nodes the user is referring to.
// If confident → return resolved entities. If ambiguous → return a clarification question.
//
// Does NOT classify intent. The resolved entity types drive context assembly.
// The conversation history is used for pronoun resolution ("that", "it", "the other one"),
// not sent to the response model.
//
// Uses Claude Haiku via Anthropic Messages API. Falls back to regex patterns
// for direct action detection (park/resume) when the API is unavailable.
//
// Depends on: Anthropic Messages API, Npgsql (for project/topic/turn queries),
//             Apache AGE (for topic edge traversal), IntentClassifier (fallback)
// Used by:    ChatService

using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace CodeStoragePoc.ContextRouter;

public class InterpreterService
{
    private readonly string _connStr;
    private readonly IntentClassifier _regexFallback;
    private readonly HttpClient _httpClient;
    private readonly string? _apiKey;
    private readonly string _model;

    public InterpreterService(string connectionString, IntentClassifier regexFallback)
    {
        _connStr = connectionString;
        _regexFallback = regexFallback;
        _apiKey = Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY");
        _model = Environment.GetEnvironmentVariable("INTERPRETER_MODEL") ?? "claude-haiku-4-5-20251001";
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };

        if (string.IsNullOrEmpty(_apiKey))
            Console.WriteLine("[WARN] ANTHROPIC_API_KEY not set — Interpreter will use regex fallback");
    }

    /// <summary>
    /// Interpret user input: resolve project, classify intent, identify entities.
    /// Falls back to regex classification if the API is unavailable.
    /// </summary>
    public async Task<InterpretedInput> InterpretAsync(
        string cleanedMessage,
        Guid? conversationId)
    {
        // Delegate to ResolveAsync and convert for backward compat
        var resolved = await ResolveAsync(cleanedMessage, conversationId);
        return ResolvedToInterpreted(resolved, cleanedMessage);
    }

    /// <summary>
    /// Resolve what the user is talking about. Returns resolved entities or a clarification question.
    /// This is the new primary entry point — InterpretAsync wraps it for backward compat.
    /// </summary>
    public async Task<ResolvedSubject> ResolveAsync(
        string cleanedMessage,
        Guid? conversationId)
    {
        var sw = Stopwatch.StartNew();

        // Detect direct actions (park/resume) — still need entity resolution from model
        // but flag the action type so ChatService can skip the response model
        var directActionType = DetectDirectActionType(cleanedMessage);

        if (string.IsNullOrEmpty(_apiKey))
        {
            var fallback = RegexFallbackResolved(cleanedMessage, sw.ElapsedMilliseconds);
            if (directActionType != null)
                return fallback with { IsDirectAction = true, DirectActionType = directActionType };
            return fallback;
        }

        try
        {
            // Gather context for resolution — all queries in parallel
            var projectsTask = GetKnownProjects();
            var topicsTask = conversationId.HasValue
                ? GetRecentTopics(conversationId.Value)
                : Task.FromResult(new List<(string name, string nodeType, Guid id)>());
            var turnsTask = conversationId.HasValue
                ? GetRecentTurns(conversationId.Value)
                : Task.FromResult(new List<(string role, string content)>());

            await Task.WhenAll(projectsTask, topicsTask, turnsTask);

            var projects = projectsTask.Result;
            var recentTopics = topicsTask.Result;
            var recentTurns = turnsTask.Result;

            // Fetch edges for topic nodes (needs topic IDs from above)
            var topicIds = recentTopics.Select(t => t.id).ToList();
            var topicEdges = topicIds.Count > 0
                ? await GetTopicEdges(topicIds)
                : new Dictionary<Guid, List<(string direction, string edgeLabel, string targetName, string targetType)>>();

            // Build the resolution prompt
            var systemPrompt = BuildResolutionPrompt(projects, recentTopics, recentTurns, topicEdges);

            // Call Claude Haiku
            var response = await CallAnthropic(systemPrompt, cleanedMessage);
            if (response == null)
                return RegexFallbackResolved(cleanedMessage, sw.ElapsedMilliseconds);

            // Parse the model's JSON response
            var resolved = ParseResolvedResponse(response, cleanedMessage, projects, sw.ElapsedMilliseconds);

            // Tag with direct action type if detected (model still resolved entities)
            if (directActionType != null)
                resolved = resolved with { IsDirectAction = true, DirectActionType = directActionType };

            return resolved;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Interpreter failed: {ex.Message}");
            var fallback = RegexFallbackResolved(cleanedMessage, sw.ElapsedMilliseconds);
            if (directActionType != null)
                return fallback with { IsDirectAction = true, DirectActionType = directActionType };
            return fallback;
        }
    }

    /// <summary>Detect park/resume action type via pattern matching.
    /// Returns the action type string, or null. Does NOT create a ResolvedSubject —
    /// the model still needs to resolve which entity the action applies to.</summary>
    private static string? DetectDirectActionType(string message)
    {
        var lower = message.ToLowerInvariant();
        string[] parkPatterns = ["park that", "park this", "shelve", "put that aside", "table that", "back burner"];
        string[] resumePatterns = ["revisit", "come back to", "let's go back to", "reopen", "un-park"];

        foreach (var p in parkPatterns)
            if (lower.Contains(p)) return "park";

        foreach (var p in resumePatterns)
            if (lower.Contains(p)) return "resume";

        return null;
    }

    /// <summary>Convert ResolvedSubject back to InterpretedInput for backward compat.</summary>
    private static InterpretedInput ResolvedToInterpreted(ResolvedSubject resolved, string cleanedMessage)
    {
        return new InterpretedInput
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
    }

    /// <summary>Build the resolution-focused prompt for the interpreter model.</summary>
    private string BuildResolutionPrompt(
        List<(Guid id, string name, int nodeCount)> projects,
        List<(string name, string nodeType, Guid id)> recentTopics,
        List<(string role, string content)> recentTurns,
        Dictionary<Guid, List<(string direction, string edgeLabel, string targetName, string targetType)>> topicEdges)
    {
        var projectList = projects.Count > 0
            ? string.Join("\n", projects.Select(p => $"  - {p.name} (id: {p.id}, {p.nodeCount} nodes)"))
            : "  (none)";

        // Build topic list with edge relationships
        var topicSb = new StringBuilder();
        if (recentTopics.Count > 0)
        {
            foreach (var t in recentTopics)
            {
                topicSb.AppendLine($"  - [{t.nodeType}] {t.name} (id: {t.id})");
                if (topicEdges.TryGetValue(t.id, out var edges))
                {
                    foreach (var e in edges)
                        topicSb.AppendLine($"      {e.direction} {e.edgeLabel} [{e.targetType}] {e.targetName}");
                }
            }
        }
        else
        {
            topicSb.AppendLine("  (none)");
        }

        // Build recent conversation turns (for pronoun resolution only)
        var turnList = recentTurns.Count > 0
            ? string.Join("\n", recentTurns.Select(t => $"  [{t.role}]: {t.content}"))
            : "  (none)";

        return $$"""
<role>
You are an entity resolver for an AI assistant. Your ONLY job is to figure out WHAT the user is talking about.

You are NOT classifying intent. You are resolving references.
</role>

<job>
Given the user's message, the conversation history, and the known entities:

1. RESOLVE ENTITIES: What specific things is the user referring to? Match to existing node IDs when possible.
2. PROJECT SCOPE: Which project context does this belong to?
3. CONFIDENCE: How sure are you about the resolution?
4. IF UNSURE: Instead of guessing, provide a clarification question and candidate matches.
5. RESPONSE GUIDANCE: How should the response model format its answer?
</job>

<known_projects>
{{projectList}}
</known_projects>

<conversation_history>
Use ONLY for resolving pronouns and references ("that", "it", "the other one").
This is NOT sent to the response model — only used to understand what the user means.
{{turnList}}
</conversation_history>

<known_entities>
Things we already track. Match user references to these when possible:
{{topicSb.ToString().TrimEnd()}}
</known_entities>

<rules>
- Your primary job is RESOLUTION, not classification. Figure out what they're referring to.
- Use conversation_history to resolve "that", "it", "the voice thing", etc.
- Use graph relationships to understand connections ("the thing that came from X").
- If a reference is ambiguous (could be 2+ entities), set resolved to false and provide candidates.
- If the user is clearly starting a NEW topic with no existing matches, that's fine — resolved=true with empty entities.
- If you genuinely can't tell what they mean, set resolved=false with a clarification question.
- Confidence below 0.5 means you should ask for clarification instead of guessing.
- NEVER guess an entity match with low confidence. It's better to ask than to be wrong.
</rules>

<response_guidance_rules>
Analyze the user's message to determine how the response model should format its answer:
- format: "bullets" for lists/options/comparisons, "prose" for explanations/stories, "code" for implementation requests, "structured" for plans/specs
- length: "brief" for simple questions or casual chat, "moderate" for most discussions, "detailed" for deep technical questions or planning
- tone: "casual" for brainstorming/chat, "technical" for implementation/architecture, "explanatory" for teaching/clarifying
</response_guidance_rules>

<response_format>
Respond with ONLY valid JSON, no markdown fences, no explanation before or after:
{
  "resolved": true | false,
  "project": {
    "type": "new" | "existing" | "cross_project",
    "name": "project name",
    "id": "existing project UUID or null"
  },
  "confidence": 0.0 to 1.0,
  "entities": [
    {
      "mention": "text the user used to refer to something",
      "match_id": "UUID of a matching entity or null",
      "match_type": "node type of the match or null"
    }
  ],
  "clarification": "question to ask the user if resolved=false, otherwise null",
  "candidates": [
    {
      "mention": "ambiguous reference",
      "match_id": "UUID of candidate",
      "match_type": "node type"
    }
  ],
  "response_guidance": {
    "format": "bullets | prose | code | structured",
    "length": "brief | moderate | detailed",
    "tone": "casual | technical | explanatory"
  },
  "reasoning": "one sentence"
}
</response_format>
""";
    }

    /// <summary>Build legacy intent-classification prompt (kept for InterpretAsync backward compat).</summary>
    private string BuildSystemPrompt(
        List<(Guid id, string name, int nodeCount)> projects,
        List<(string name, string nodeType, Guid id)> recentTopics,
        List<(string role, string content)> recentTurns,
        Dictionary<Guid, List<(string direction, string edgeLabel, string targetName, string targetType)>> topicEdges)
    {
        // Delegate to new prompt — the old InterpretAsync wraps ResolveAsync now
        return BuildResolutionPrompt(projects, recentTopics, recentTurns, topicEdges);
    }

    private async Task<string?> CallAnthropic(string systemPrompt, string userMessage)
    {
        try
        {
            var requestBody = new
            {
                model = _model,
                max_tokens = 400,
                system = systemPrompt,
                messages = new[]
                {
                    new { role = "user", content = userMessage }
                }
            };

            var json = JsonSerializer.Serialize(requestBody);
            var request = new HttpRequestMessage(HttpMethod.Post, "https://api.anthropic.com/v1/messages")
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json")
            };
            request.Headers.Add("x-api-key", _apiKey);
            request.Headers.Add("anthropic-version", "2023-06-01");

            var response = await _httpClient.SendAsync(request);
            response.EnsureSuccessStatusCode();

            using var responseDoc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync());
            var content = responseDoc.RootElement.GetProperty("content");
            if (content.GetArrayLength() > 0)
            {
                var textBlock = content[0];
                if (textBlock.GetProperty("type").GetString() == "text")
                    return textBlock.GetProperty("text").GetString();
            }

            return null;
        }
        catch (TaskCanceledException)
        {
            Console.WriteLine("[WARN] Interpreter API call timed out after 15s");
            return null;
        }
        catch (HttpRequestException ex)
        {
            Console.WriteLine($"[WARN] Interpreter API request failed: {ex.Message}");
            return null;
        }
    }

    private InterpretedInput ParseResponse(
        string rawResponse,
        string cleanedMessage,
        List<(Guid id, string name, int nodeCount)> projects,
        long durationMs)
    {
        try
        {
            // Strip markdown fences if present (defensive)
            var jsonStr = rawResponse.Trim();
            if (jsonStr.StartsWith("```"))
            {
                var firstNewline = jsonStr.IndexOf('\n');
                var lastFence = jsonStr.LastIndexOf("```");
                if (firstNewline >= 0 && lastFence > firstNewline)
                    jsonStr = jsonStr[(firstNewline + 1)..lastFence].Trim();
            }

            // Strip any preamble text before the JSON
            var braceIndex = jsonStr.IndexOf('{');
            if (braceIndex > 0)
                jsonStr = jsonStr[braceIndex..];

            using var doc = JsonDocument.Parse(jsonStr);
            var root = doc.RootElement;

            // Parse project
            var projectEl = root.GetProperty("project");
            var projectType = projectEl.GetProperty("type").GetString() ?? "existing";
            var projectName = projectEl.TryGetProperty("name", out var pn) ? pn.GetString() : null;
            Guid? projectId = null;

            if (projectEl.TryGetProperty("id", out var pidEl) && pidEl.ValueKind == JsonValueKind.String)
            {
                var pidStr = pidEl.GetString();
                if (!string.IsNullOrEmpty(pidStr) && pidStr != "null" && Guid.TryParse(pidStr, out var parsed))
                    projectId = parsed;
            }

            // If the model said "existing" but gave no valid ID, try to match by name
            if (projectType == "existing" && projectId == null && projectName != null)
            {
                var match = projects.FirstOrDefault(p =>
                    p.name.Equals(projectName, StringComparison.OrdinalIgnoreCase));
                if (match.id != Guid.Empty)
                    projectId = match.id;
            }

            // Parse intent
            var intentStr = root.GetProperty("intent").GetString() ?? "ideation";
            var intent = ParseIntent(intentStr);

            // Parse confidence
            var confidence = root.TryGetProperty("confidence", out var confEl)
                ? confEl.GetDouble()
                : 0.7;

            // Parse entities
            var entities = new List<ResolvedEntity>();
            if (root.TryGetProperty("entities", out var entitiesEl) && entitiesEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var entityEl in entitiesEl.EnumerateArray())
                {
                    var mention = entityEl.TryGetProperty("mention", out var m) ? m.GetString() ?? "" : "";
                    Guid? matchId = null;
                    if (entityEl.TryGetProperty("match_id", out var mid) && mid.ValueKind == JsonValueKind.String)
                    {
                        var midStr = mid.GetString();
                        if (!string.IsNullOrEmpty(midStr) && midStr != "null" && Guid.TryParse(midStr, out var parsedMid))
                            matchId = parsedMid;
                    }
                    if (!string.IsNullOrEmpty(mention))
                    {
                        entities.Add(new ResolvedEntity
                        {
                            Mention = mention,
                            NodeId = matchId
                        });
                    }
                }
            }

            // Parse reasoning
            var reasoning = root.TryGetProperty("reasoning", out var reasonEl)
                ? reasonEl.GetString()
                : null;

            // Parse response guidance
            ResponseGuidance? responseGuidance = null;
            if (root.TryGetProperty("response_guidance", out var guidanceEl) && guidanceEl.ValueKind == JsonValueKind.Object)
            {
                var format = guidanceEl.TryGetProperty("format", out var fmtEl) ? fmtEl.GetString() ?? "prose" : "prose";
                var length = guidanceEl.TryGetProperty("length", out var lenEl) ? lenEl.GetString() ?? "moderate" : "moderate";
                var tone = guidanceEl.TryGetProperty("tone", out var toneEl) ? toneEl.GetString() ?? "casual" : "casual";
                responseGuidance = new ResponseGuidance(format, length, tone);
            }

            return new InterpretedInput
            {
                Project = new ProjectResolution
                {
                    Type = projectType,
                    ProjectId = projectId,
                    ProjectName = projectName
                },
                Intent = intent,
                Confidence = confidence,
                Entities = entities,
                CleanedMessage = cleanedMessage,
                Reasoning = reasoning,
                DurationMs = durationMs,
                IsRegexFallback = false,
                ResponseGuidance = responseGuidance
            };
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Interpreter JSON parse failed: {ex.Message}");
            Console.WriteLine($"[WARN] Raw response: {rawResponse[..Math.Min(rawResponse.Length, 200)]}");
            return RegexFallback(cleanedMessage, durationMs);
        }
    }

    /// <summary>Parse the model's resolution-focused JSON response into a ResolvedSubject.</summary>
    private ResolvedSubject ParseResolvedResponse(
        string rawResponse,
        string cleanedMessage,
        List<(Guid id, string name, int nodeCount)> projects,
        long durationMs)
    {
        try
        {
            // Strip markdown fences and preamble (defensive)
            var jsonStr = rawResponse.Trim();
            if (jsonStr.StartsWith("```"))
            {
                var firstNewline = jsonStr.IndexOf('\n');
                var lastFence = jsonStr.LastIndexOf("```");
                if (firstNewline >= 0 && lastFence > firstNewline)
                    jsonStr = jsonStr[(firstNewline + 1)..lastFence].Trim();
            }
            var braceIndex = jsonStr.IndexOf('{');
            if (braceIndex > 0)
                jsonStr = jsonStr[braceIndex..];

            using var doc = JsonDocument.Parse(jsonStr);
            var root = doc.RootElement;

            // Parse resolved flag
            var isResolved = root.TryGetProperty("resolved", out var resolvedEl)
                ? resolvedEl.GetBoolean()
                : true; // default to resolved for backward compat

            // Parse confidence
            var confidence = root.TryGetProperty("confidence", out var confEl)
                ? confEl.GetDouble()
                : 0.7;

            // If confidence is below threshold, treat as unresolved
            if (confidence < 0.5 && isResolved)
                isResolved = false;

            // Parse project
            var projectEl = root.GetProperty("project");
            var projectType = projectEl.GetProperty("type").GetString() ?? "existing";
            var projectName = projectEl.TryGetProperty("name", out var pn) ? pn.GetString() : null;
            Guid? projectId = null;

            if (projectEl.TryGetProperty("id", out var pidEl) && pidEl.ValueKind == JsonValueKind.String)
            {
                var pidStr = pidEl.GetString();
                if (!string.IsNullOrEmpty(pidStr) && pidStr != "null" && Guid.TryParse(pidStr, out var parsed))
                    projectId = parsed;
            }

            if (projectType == "existing" && projectId == null && projectName != null)
            {
                var match = projects.FirstOrDefault(p =>
                    p.name.Equals(projectName, StringComparison.OrdinalIgnoreCase));
                if (match.id != Guid.Empty)
                    projectId = match.id;
            }

            // Parse entities
            var entities = new List<ResolvedEntity>();
            var subjectTypes = new HashSet<string>();
            if (root.TryGetProperty("entities", out var entitiesEl) && entitiesEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var entityEl in entitiesEl.EnumerateArray())
                {
                    var mention = entityEl.TryGetProperty("mention", out var m) ? m.GetString() ?? "" : "";
                    Guid? matchId = null;
                    string? matchType = null;

                    if (entityEl.TryGetProperty("match_id", out var mid) && mid.ValueKind == JsonValueKind.String)
                    {
                        var midStr = mid.GetString();
                        if (!string.IsNullOrEmpty(midStr) && midStr != "null" && Guid.TryParse(midStr, out var parsedMid))
                            matchId = parsedMid;
                    }
                    if (entityEl.TryGetProperty("match_type", out var mt) && mt.ValueKind == JsonValueKind.String)
                        matchType = mt.GetString();

                    if (!string.IsNullOrEmpty(mention))
                    {
                        entities.Add(new ResolvedEntity
                        {
                            Mention = mention,
                            NodeId = matchId,
                            NodeType = matchType
                        });
                        if (matchType != null)
                            subjectTypes.Add(matchType);
                    }
                }
            }

            // Parse clarification + candidates
            var clarification = root.TryGetProperty("clarification", out var clarEl) && clarEl.ValueKind == JsonValueKind.String
                ? clarEl.GetString()
                : null;

            var candidates = new List<ResolvedEntity>();
            if (root.TryGetProperty("candidates", out var candsEl) && candsEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var candEl in candsEl.EnumerateArray())
                {
                    var mention = candEl.TryGetProperty("mention", out var cm) ? cm.GetString() ?? "" : "";
                    Guid? matchId = null;
                    string? matchType = null;
                    if (candEl.TryGetProperty("match_id", out var cmid) && cmid.ValueKind == JsonValueKind.String)
                    {
                        var cmidStr = cmid.GetString();
                        if (!string.IsNullOrEmpty(cmidStr) && cmidStr != "null" && Guid.TryParse(cmidStr, out var parsedCmid))
                            matchId = parsedCmid;
                    }
                    if (candEl.TryGetProperty("match_type", out var cmt) && cmt.ValueKind == JsonValueKind.String)
                        matchType = cmt.GetString();

                    candidates.Add(new ResolvedEntity { Mention = mention, NodeId = matchId, NodeType = matchType });
                }
            }

            // Parse reasoning
            var reasoning = root.TryGetProperty("reasoning", out var reasonEl)
                ? reasonEl.GetString()
                : null;

            // Parse response guidance
            ResponseGuidance? responseGuidance = null;
            if (root.TryGetProperty("response_guidance", out var guidanceEl) && guidanceEl.ValueKind == JsonValueKind.Object)
            {
                var format = guidanceEl.TryGetProperty("format", out var fmtEl) ? fmtEl.GetString() ?? "prose" : "prose";
                var length = guidanceEl.TryGetProperty("length", out var lenEl) ? lenEl.GetString() ?? "moderate" : "moderate";
                var tone = guidanceEl.TryGetProperty("tone", out var toneEl) ? toneEl.GetString() ?? "casual" : "casual";
                responseGuidance = new ResponseGuidance(format, length, tone);
            }

            return new ResolvedSubject
            {
                IsResolved = isResolved,
                Entities = entities,
                SubjectTypes = subjectTypes,
                Confidence = confidence,
                ClarificationQuestion = clarification,
                CandidateMatches = candidates,
                Project = new ProjectResolution
                {
                    Type = projectType,
                    ProjectId = projectId,
                    ProjectName = projectName
                },
                ResponseGuidance = responseGuidance,
                CleanedMessage = cleanedMessage,
                DurationMs = durationMs,
                IsRegexFallback = false,
                Reasoning = reasoning
            };
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Resolver JSON parse failed: {ex.Message}");
            Console.WriteLine($"[WARN] Raw response: {rawResponse[..Math.Min(rawResponse.Length, 200)]}");
            return RegexFallbackResolved(cleanedMessage, durationMs);
        }
    }

    /// <summary>Fallback when API is unavailable — minimal resolution, no entity matching.</summary>
    private ResolvedSubject RegexFallbackResolved(string cleanedMessage, long durationMs)
    {
        return new ResolvedSubject
        {
            IsResolved = true, // Assume resolved so the pipeline continues
            Confidence = 0.3,  // Low confidence signals degraded mode
            CleanedMessage = cleanedMessage,
            Reasoning = "Regex fallback: API unavailable, no entity resolution",
            DurationMs = durationMs,
            IsRegexFallback = true,
            Project = new ProjectResolution { Type = "existing" }
        };
    }

    private static Intent ParseIntent(string intentStr)
    {
        return intentStr.ToLowerInvariant() switch
        {
            "ideation" => Intent.Ideation,
            "deepening" => Intent.Deepening,
            "planning" => Intent.Planning,
            "reviewing" => Intent.Reviewing,
            "executing" => Intent.Executing,
            "recalling" => Intent.Recalling,
            "parking" => Intent.Parking,
            "resuming" => Intent.Resuming,
            _ => Intent.Ideation
        };
    }

    private InterpretedInput RegexFallback(string cleanedMessage, long durationMs)
    {
        var result = _regexFallback.Classify(cleanedMessage);
        return new InterpretedInput
        {
            Project = new ProjectResolution { Type = "existing" },
            Intent = result.Intent,
            Confidence = result.Confidence == Confidence.High ? 0.9 : 0.5,
            CleanedMessage = cleanedMessage,
            Reasoning = $"Regex fallback: matched pattern \"{result.MatchedPattern ?? "default"}\"",
            DurationMs = durationMs,
            IsRegexFallback = true
        };
    }

    /// <summary>Get recent conversation turns (last 5) for contextual interpretation.</summary>
    private async Task<List<(string role, string content)>> GetRecentTurns(Guid conversationId)
    {
        var results = new List<(string, string)>();
        try
        {
            await using var conn = new NpgsqlConnection(_connStr);
            await conn.OpenAsync();

            await using var cmd = new NpgsqlCommand("""
                SELECT
                    COALESCE(na.value, 'user') as role,
                    LEFT(COALESCE(n.value, n.name, ''), 200) as content
                FROM nodes n
                LEFT JOIN node_attributes na ON na.node_id = n.id AND na.key = 'role'
                WHERE n.node_type = 'turn'
                  AND (n.parent_id = @convId
                       OR n.parent_id IN (SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'))
                ORDER BY n.created_at DESC
                LIMIT 5
            """, conn);
            cmd.Parameters.AddWithValue("convId", conversationId);

            await using var reader = await cmd.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                results.Add((
                    reader.GetString(0),
                    reader.GetString(1)
                ));
            }

            // Reverse so oldest is first (chronological order)
            results.Reverse();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Failed to load recent turns for interpreter: {ex.Message}");
        }
        return results;
    }

    /// <summary>
    /// Get AGE graph edges for a set of topic nodes (relationships to other nodes).
    /// Single batched bidirectional query — fetches both incoming and outgoing edges.
    /// </summary>
    private async Task<Dictionary<Guid, List<(string direction, string edgeLabel, string targetName, string targetType)>>> GetTopicEdges(
        List<Guid> topicIds)
    {
        var results = new Dictionary<Guid, List<(string, string, string, string)>>();
        if (topicIds.Count == 0) return results;

        try
        {
            await using var conn = new NpgsqlConnection(_connStr);
            await conn.OpenAsync();

            // Prepare AGE
            await using var load = new NpgsqlCommand("LOAD 'age';", conn);
            await load.ExecuteNonQueryAsync();
            await using var path = new NpgsqlCommand(
                "SET search_path = ag_catalog, \"$user\", public;", conn);
            await path.ExecuteNonQueryAsync();

            // Build IN list for batched query (UUIDs are safe — no injection risk)
            var inList = string.Join(", ", topicIds.Select(id => $"'{id}'"));
            var cypher = $"MATCH (a)-[e]-(b) WHERE a.node_id IN [{inList}] RETURN a, e, b LIMIT 50";
            var inner = $"SELECT * FROM cypher('code_graph', $$ {cypher} $$) as (a agtype, e agtype, b agtype)";
            var sql = $"SELECT a::text, e::text, b::text FROM ({inner}) sub;";

            await using var cmd = new NpgsqlCommand(sql, conn);
            await using var reader = await cmd.ExecuteReaderAsync();

            while (await reader.ReadAsync())
            {
                var sourceText = reader.GetString(0);
                var edgeText = reader.GetString(1);
                var targetText = reader.GetString(2);

                var parsed = ParseEdgeRow(sourceText, edgeText, targetText);
                if (parsed == null) continue;

                var (sourceId, direction, edgeLabel, targetName, targetType) = parsed.Value;

                if (!results.TryGetValue(sourceId, out var edges))
                {
                    edges = new List<(string, string, string, string)>();
                    results[sourceId] = edges;
                }

                // Cap at 5 edges per topic
                if (edges.Count < 5)
                    edges.Add((direction, edgeLabel, targetName, targetType));
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Failed to load topic edges for interpreter: {ex.Message}");
        }
        return results;
    }

    /// <summary>
    /// Parse a single edge row from AGE agtype strings.
    /// Returns source node ID, direction arrow, edge label, target name, target type.
    /// Parses each agtype string exactly once.
    /// </summary>
    private static (Guid sourceId, string direction, string edgeLabel, string targetName, string targetType)?
        ParseEdgeRow(string sourceAgtype, string edgeAgtype, string targetAgtype)
    {
        try
        {
            // Parse source vertex — extract node_id
            var sourceProps = ParseAgtypeProperties(sourceAgtype);
            if (sourceProps == null || !sourceProps.TryGetValue("node_id", out var sourceIdStr))
                return null;
            if (!Guid.TryParse(sourceIdStr, out var sourceId))
                return null;

            // Parse edge — extract label + start_id/end_id for direction
            var edgeJson = StripAgtypeSuffix(edgeAgtype);
            using var edgeDoc = JsonDocument.Parse(edgeJson);
            var edgeRoot = edgeDoc.RootElement;
            var edgeLabel = edgeRoot.TryGetProperty("label", out var lbl) ? lbl.GetString() ?? "RELATED" : "RELATED";

            // Determine direction: if edge end_id matches source vertex id, it's incoming
            var direction = "→"; // outgoing by default
            if (edgeRoot.TryGetProperty("end_id", out var endId) && edgeRoot.TryGetProperty("start_id", out var startId))
            {
                // Parse source vertex id (AGE internal id, not node_id)
                var sourceJson = StripAgtypeSuffix(sourceAgtype);
                using var sourceDoc = JsonDocument.Parse(sourceJson);
                var sourceVertexId = sourceDoc.RootElement.TryGetProperty("id", out var sid) ? sid.GetInt64() : -1;

                if (endId.GetInt64() == sourceVertexId)
                    direction = "←"; // incoming edge
            }

            // Parse target vertex — extract name and type from properties
            var targetProps = ParseAgtypeProperties(targetAgtype);
            var targetName = targetProps?.GetValueOrDefault("name") ?? "unknown";
            var targetType = targetProps?.GetValueOrDefault("type") ?? "node";

            return (sourceId, direction, edgeLabel, targetName, targetType);
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Strip the ::vertex or ::edge suffix from an AGE agtype string.</summary>
    private static string StripAgtypeSuffix(string agtype)
    {
        var suffixIdx = agtype.LastIndexOf("::");
        return suffixIdx > 0 ? agtype[..suffixIdx] : agtype;
    }

    /// <summary>Parse the properties sub-object from an AGE agtype vertex/edge string.</summary>
    private static Dictionary<string, string>? ParseAgtypeProperties(string agtype)
    {
        try
        {
            var json = StripAgtypeSuffix(agtype);
            using var doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("properties", out var props))
                return null;

            var result = new Dictionary<string, string>();
            foreach (var prop in props.EnumerateObject())
            {
                if (prop.Value.ValueKind == JsonValueKind.String)
                    result[prop.Name] = prop.Value.GetString()!;
            }
            return result;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Get all projects with node counts for the interpreter's context.</summary>
    private async Task<List<(Guid id, string name, int nodeCount)>> GetKnownProjects()
    {
        var results = new List<(Guid, string, int)>();
        try
        {
            await using var conn = new NpgsqlConnection(_connStr);
            await conn.OpenAsync();

            await using var cmd = new NpgsqlCommand("""
                SELECT p.id, p.name, COUNT(n.id) as node_count
                FROM projects p
                LEFT JOIN nodes n ON n.project_id = p.id
                GROUP BY p.id, p.name
                ORDER BY COUNT(n.id) DESC
            """, conn);

            await using var reader = await cmd.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                results.Add((
                    reader.GetGuid(0),
                    reader.GetString(1),
                    (int)reader.GetInt64(2)
                ));
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Failed to load projects for interpreter: {ex.Message}");
        }
        return results;
    }

    /// <summary>Get recent topics/ideas/threads from the current conversation for entity resolution.</summary>
    private async Task<List<(string name, string nodeType, Guid id)>> GetRecentTopics(Guid conversationId)
    {
        var results = new List<(string, string, Guid)>();
        try
        {
            await using var conn = new NpgsqlConnection(_connStr);
            await conn.OpenAsync();

            await using var cmd = new NpgsqlCommand("""
                SELECT n.name, n.node_type, n.id
                FROM nodes n
                WHERE n.node_type IN ('idea', 'question', 'decision', 'thread', 'topic')
                  AND (n.parent_id = @convId
                       OR n.parent_id IN (SELECT id FROM nodes WHERE parent_id = @convId))
                  AND n.name IS NOT NULL
                ORDER BY n.modified_at DESC
                LIMIT 10
            """, conn);
            cmd.Parameters.AddWithValue("convId", conversationId);

            await using var reader = await cmd.ExecuteReaderAsync();
            while (await reader.ReadAsync())
            {
                results.Add((
                    reader.GetString(0),
                    reader.GetString(1),
                    reader.GetGuid(2)
                ));
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[WARN] Failed to load recent topics for interpreter: {ex.Message}");
        }
        return results;
    }
}
