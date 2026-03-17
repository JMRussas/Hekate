// CodeStoragePoc - Context Assembler
//
// Assembles state around what the user is talking about.
//
// Two modes:
// 1. Entity-driven (new): ResolvedSubject → SubjectState
//    The resolved entity types determine what to pull. Plan nodes get plan state.
//    Idea nodes get idea subtrees. No entities → semantic search + open items.
//
// 2. Intent-driven (legacy): Intent → ContextPayload
//    Kept for backward compat with PreviewAsync and Program.cs demo.
//
// Core principle: send the model the STATE of things, not a replay of how we got there.
// No chat history in context — just the subject, its connections, and what's open.
//
// Depends on: NodeRepository, EmbeddingService, AgeLayer, Npgsql, Types.cs
// Used by:    ChatService, AgentDispatcher

using CodeStoragePoc.DbLayer;
using CodeStoragePoc.GraphLayer;
using Npgsql;

namespace CodeStoragePoc.ContextRouter;

public class ContextAssembler
{
    private readonly NodeRepository _repo;
    private readonly string _connStr;
    private readonly EmbeddingService? _embeddingService;
    private readonly AgeLayer? _ageLayer;

    public ContextAssembler(NodeRepository repo, string connectionString, EmbeddingService? embeddingService = null, AgeLayer? ageLayer = null)
    {
        _repo = repo;
        _connStr = connectionString;
        _embeddingService = embeddingService;
        _ageLayer = ageLayer;
    }

    /// <summary>
    /// Assemble state around a resolved subject — the new primary entry point.
    /// Entity types drive what context is pulled. No intent switch statement.
    /// </summary>
    public async Task<SubjectState> AssembleFromSubject(
        ResolvedSubject subject,
        Guid conversationId)
    {
        // Get resolved node IDs for graph traversal seeds
        var resolvedNodeIds = subject.Entities
            .Where(e => e.NodeId.HasValue)
            .Select(e => e.NodeId!.Value)
            .ToList();

        // Parallel: stats, coverage, node states, graph neighbors, semantic search, recent turns
        var statsTask = GetConversationStats(conversationId);
        var coverageTask = GetSearchCoverage();
        var nodeStatesTask = resolvedNodeIds.Count > 0
            ? GetNodeStates(resolvedNodeIds)
            : Task.FromResult(new List<SubjectNodeState>());
        var semanticTask = SemanticSearchOrFallback(
            subject.CleanedMessage, conversationId, nodeTypes: null, limit: 5);
        var recentTurnsTask = GetRecentTurns(conversationId, limit: 4);

        // Graph neighbors: adapt hops and edge filters based on subject types
        var graphTask = resolvedNodeIds.Count > 0 && _ageLayer != null
            ? GetGraphContext(resolvedNodeIds, maxHops: 2, limit: 10,
                edgeTypeFilter: GetRelevantEdgeTypes(subject.SubjectTypes))
            : Task.FromResult(new List<ContextItem>());

        // Open items: blockers, open questions relevant to the subject
        var openItemsTask = GetOpenItems(conversationId, subject.SubjectTypes);

        await Task.WhenAll(statsTask, coverageTask, nodeStatesTask, graphTask, semanticTask, openItemsTask, recentTurnsTask);

        var stats = statsTask.Result;
        var graphResults = graphTask.Result;
        var semanticResults = semanticTask.Result;

        // Deduplicate: remove semantic results that are already in graph results
        var graphNodeIds = graphResults.Select(g => g.NodeId).ToHashSet();
        var uniqueSemanticResults = semanticResults
            .Where(s => !graphNodeIds.Contains(s.NodeId) && !resolvedNodeIds.Contains(s.NodeId))
            .ToList();

        // Build the agent contract based on subject types
        var contract = BuildContract(subject);

        return new SubjectState
        {
            Subject = subject,
            ResolvedNodeStates = nodeStatesTask.Result,
            ConnectedNodes = graphResults,
            RelatedNodes = uniqueSemanticResults,
            OpenItems = openItemsTask.Result,
            RecentTurns = recentTurnsTask.Result,
            TotalIdeas = stats.TotalIdeas,
            OpenQuestions = stats.OpenQuestions,
            ParkedIdeas = stats.ParkedIdeas,
            Coverage = coverageTask.Result,
            ResponseGuidance = subject.ResponseGuidance,
            Contract = contract
        };
    }

    /// <summary>Get full state of resolved nodes — attributes, child summary.
    /// Batched: 3 queries total regardless of node count (not N+1).</summary>
    private async Task<List<SubjectNodeState>> GetNodeStates(List<Guid> nodeIds)
    {
        if (nodeIds.Count == 0) return new List<SubjectNodeState>();

        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Build parameterized IN clause
        var idParams = string.Join(",", nodeIds.Select((_, i) => $"@id{i}"));
        void AddIdParams(NpgsqlCommand cmd)
        {
            for (int i = 0; i < nodeIds.Count; i++)
                cmd.Parameters.AddWithValue($"id{i}", nodeIds[i]);
        }

        // Query 1: nodes
        await using var nodeCmd = new NpgsqlCommand($"""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 1000) as value
            FROM nodes n WHERE n.id IN ({idParams})
        """, conn);
        AddIdParams(nodeCmd);

        var nodeMap = new Dictionary<Guid, SubjectNodeState>();
        await using (var reader = await nodeCmd.ExecuteReaderAsync())
        {
            while (await reader.ReadAsync())
            {
                var id = reader.GetGuid(0);
                nodeMap[id] = new SubjectNodeState
                {
                    NodeId = id,
                    NodeType = reader.GetString(1),
                    Name = reader.IsDBNull(2) ? null : reader.GetString(2),
                    Value = reader.IsDBNull(3) ? null : reader.GetString(3)
                };
            }
        }

        if (nodeMap.Count == 0) return new List<SubjectNodeState>();

        // Query 2: all attributes for all nodes
        var attrMap = new Dictionary<Guid, Dictionary<string, string>>();
        await using var attrCmd = new NpgsqlCommand($"""
            SELECT node_id, key, value FROM node_attributes WHERE node_id IN ({idParams})
        """, conn);
        AddIdParams(attrCmd);

        await using (var attrReader = await attrCmd.ExecuteReaderAsync())
        {
            while (await attrReader.ReadAsync())
            {
                var nid = attrReader.GetGuid(0);
                if (!attrMap.TryGetValue(nid, out var attrs))
                {
                    attrs = new Dictionary<string, string>();
                    attrMap[nid] = attrs;
                }
                attrs[attrReader.GetString(1)] = attrReader.GetString(2);
            }
        }

        // Query 3: child type summaries for all nodes
        var childMap = new Dictionary<Guid, (int count, List<string> types)>();
        await using var childCmd = new NpgsqlCommand($"""
            SELECT parent_id, node_type, COUNT(*) FROM nodes
            WHERE parent_id IN ({idParams}) GROUP BY parent_id, node_type
        """, conn);
        AddIdParams(childCmd);

        await using (var childReader = await childCmd.ExecuteReaderAsync())
        {
            while (await childReader.ReadAsync())
            {
                var pid = childReader.GetGuid(0);
                if (!childMap.TryGetValue(pid, out var entry))
                {
                    entry = (0, new List<string>());
                    childMap[pid] = entry;
                }
                var cnt = (int)childReader.GetInt64(2);
                entry.types.Add($"{childReader.GetString(1)}({cnt})");
                childMap[pid] = (entry.count + cnt, entry.types);
            }
        }

        // Assemble results in original order
        var results = new List<SubjectNodeState>();
        foreach (var nodeId in nodeIds)
        {
            if (!nodeMap.TryGetValue(nodeId, out var node)) continue;
            var attrs = attrMap.GetValueOrDefault(nodeId, new Dictionary<string, string>());
            var children = childMap.TryGetValue(nodeId, out var childEntry)
                ? childEntry
                : (count: 0, types: new List<string>());

            results.Add(node with
            {
                Status = attrs.GetValueOrDefault("status"),
                Attributes = attrs,
                ChildCount = children.count,
                ChildTypes = children.types
            });
        }

        return results;
    }

    /// <summary>Get open blockers, questions, stale items relevant to the subject domain.</summary>
    private async Task<List<ContextItem>> GetOpenItems(Guid conversationId, HashSet<string> subjectTypes)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Determine which open item types to pull based on subject domain
        var openTypes = new List<string> { "question", "idea" }; // always relevant
        if (subjectTypes.Any(t => t is "plan" or "plan_step" or "task" or "milestone"))
        {
            openTypes.AddRange(new[] { "blocker", "risk" });
        }

        var typePlaceholders = string.Join(",", openTypes.Select((_, i) => $"@type{i}"));
        await using var cmd = new NpgsqlCommand($"""
            SELECT n.id, n.node_type, n.name, n.value,
                   a.value as status
            FROM nodes n
            JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.node_type IN ({typePlaceholders})
              AND a.value IN ('mentioned', 'explored', 'open', 'blocking')
              AND (n.parent_id IN (
                  SELECT id FROM nodes WHERE parent_id = @convId
              ) OR n.parent_id = @convId)
            ORDER BY n.modified_at DESC
            LIMIT 10
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        for (int i = 0; i < openTypes.Count; i++)
            cmd.Parameters.AddWithValue($"type{i}", openTypes[i]);

        return await ReadContextItems(cmd);
    }

    /// <summary>Determine which edge types are relevant based on subject types.</summary>
    private static HashSet<string>? GetRelevantEdgeTypes(HashSet<string> subjectTypes)
    {
        // Plan domain
        if (subjectTypes.Any(t => t is "plan" or "plan_step" or "plan_phase" or "task" or "milestone"))
            return new HashSet<string> { "IMPLEMENTED_BY", "BLOCKS", "CONSTRAINS", "SPAWNED_FROM", "RELATES_TO", "PRODUCES" };

        // Code domain
        if (subjectTypes.Any(t => t is "method" or "class" or "struct" or "compilation_unit" or "namespace"))
            return new HashSet<string> { "CALLS", "REFERENCES", "DEPENDS_ON", "IMPLEMENTED_BY", "MODIFIES" };

        // Research domain
        if (subjectTypes.Any(t => t is "research" or "finding" or "open_question"))
            return new HashSet<string> { "INFORMS", "RELATES_TO", "CONTRADICTS" };

        // No filter — get everything
        return null;
    }

    /// <summary>Build an agent contract based on the resolved subject types and specific entities.</summary>
    private static AgentContract BuildContract(ResolvedSubject subject)
    {
        var types = subject.SubjectTypes;
        var entityNames = subject.Entities
            .Where(e => e.NodeName != null || e.Mention != null)
            .Select(e => e.NodeName ?? e.Mention)
            .ToList();
        var entityRef = entityNames.Count > 0
            ? $" Specifically: {string.Join(", ", entityNames)}."
            : "";

        // Plan domain
        if (types.Any(t => t is "plan" or "plan_step" or "plan_phase" or "task"))
            return new AgentContract
            {
                Function = $"Help the user work with their plan.{entityRef}",
                InputDescription = "Plan nodes with current status, blockers, connected tasks, and acceptance criteria.",
                OutputDescription = "Concrete observations about the plan state, risks, or suggested next steps.",
                Constraints = new List<string>
                {
                    "Don't introduce topics outside the plan scope.",
                    "If information is missing to evaluate a step, say what's missing."
                }
            };

        // Code domain
        if (types.Any(t => t is "method" or "class" or "struct" or "compilation_unit"))
            return new AgentContract
            {
                Function = $"Help the user understand or modify code.{entityRef}",
                InputDescription = "Code nodes with signatures, call/reference relationships, and implementing plan steps.",
                OutputDescription = "Code explanations, implementation suggestions, or actual code.",
                Constraints = new List<string>
                {
                    "Stay focused on the specific code elements referenced.",
                    "If the code context is insufficient, say what's needed."
                }
            };

        // Idea/decision domain
        if (types.Any(t => t is "idea" or "question" or "decision"))
            return new AgentContract
            {
                Function = $"Think with the user about their idea.{entityRef}",
                InputDescription = "Ideas, questions, and decisions with their current status and connections.",
                OutputDescription = "Thoughtful engagement — questions, pushback, development, or concrete next steps.",
                Constraints = new List<string>
                {
                    "Engage with what's there, don't redirect to something else.",
                    "If the idea is unclear, ask what specifically they want to explore."
                }
            };

        // Research domain
        if (types.Any(t => t is "research" or "finding" or "open_question"))
            return new AgentContract
            {
                Function = $"Help the user evaluate research findings.{entityRef}",
                InputDescription = "Research documents, findings, open questions, and cross-references.",
                OutputDescription = "Analysis, critique, synthesis, or identification of gaps.",
                Constraints = new List<string>
                {
                    "Reference specific findings when drawing conclusions.",
                    "Flag when evidence is insufficient."
                }
            };

        // Default — no specific subject type (new topic or broad question)
        return new AgentContract
        {
            Function = "Help the user with their request.",
            InputDescription = "The user's message and any related context from the knowledge store.",
            OutputDescription = "A helpful response appropriate to the request.",
            Constraints = new List<string>
            {
                "If you're unsure what the user is referring to, ask for clarification.",
                "Don't assume context that isn't provided."
            }
        };
    }

    /// <summary>
    /// Assemble context from an InterpretedInput (graph-first path).
    /// Resolved entities seed graph traversal; semantic search supplements; recency is fallback.
    /// </summary>
    public async Task<ContextPayload> Assemble(
        InterpretedInput interpreted,
        string userInput,
        Guid conversationId,
        string inputMode = "text")
    {
        var payload = await AssembleCore(interpreted.Intent, userInput, conversationId, inputMode, interpreted.Entities);
        // Pass through response guidance from the interpreter
        return payload with { ResponseGuidance = interpreted.ResponseGuidance };
    }

    /// <summary>
    /// Assemble context from a legacy IntentResult (recency-only path).
    /// Used by Program.cs demo and PreviewAsync regex fallback.
    /// </summary>
    public Task<ContextPayload> Assemble(
        IntentResult intentResult,
        string userInput,
        Guid conversationId,
        string inputMode = "text")
    {
        return AssembleCore(intentResult.Intent, userInput, conversationId, inputMode, resolvedEntities: null);
    }

    private async Task<ContextPayload> AssembleCore(
        Intent intent,
        string userInput,
        Guid conversationId,
        string inputMode,
        List<ResolvedEntity>? resolvedEntities)
    {
        var payload = new ContextPayload
        {
            Intent = intent,
            UserInput = userInput,
            InputMode = inputMode,
            ConversationId = conversationId
        };

        // Extract seed node IDs from resolved entities (if any)
        var seedNodeIds = resolvedEntities?
            .Where(e => e.NodeId.HasValue)
            .Select(e => e.NodeId!.Value)
            .ToList() ?? new List<Guid>();

        // Run stats, coverage, baseline, and intent-specific queries in parallel.
        // Each query opens its own connection, so they're safe to parallelize.
        var statsTask = GetConversationStats(conversationId);
        var coverageTask = GetSearchCoverage();
        var baselineTask = GetTotalConversationChars(conversationId);
        var ideaTypes = new[] { "idea", "question", "decision", "finding" };

        // Three-tier context strategy:
        //   Tier 1: Graph neighbors of resolved entities (structurally connected)
        //   Tier 2: Semantic search (conceptually related, may cross project boundaries)
        //   Tier 3: Recency fallback (when no entities resolved and no embeddings)
        var hasGraphSeeds = seedNodeIds.Count > 0 && _ageLayer != null;

        switch (intent)
        {
            case Intent.Ideation:
            {
                var threadsTask = GetOpenThreads(conversationId);
                var turnsTask = GetRecentTurns(conversationId, limit: 3);
                var crossTask = SemanticSearchOrFallback(userInput, conversationId, nodeTypes: null, limit: 3);

                // Tier 1: graph neighbors, or Tier 3: recency fallback
                var graphTask = hasGraphSeeds
                    ? GetGraphContext(seedNodeIds, maxHops: 1, limit: 5)
                    : GetRecentIdeas(conversationId, limit: 5);

                await Task.WhenAll(statsTask, coverageTask, baselineTask, graphTask, threadsTask, turnsTask, crossTask);
                var stats = statsTask.Result;

                // If graph returned too few results, supplement with recency
                var relevantIdeas = graphTask.Result;
                if (hasGraphSeeds && relevantIdeas.Count < 3)
                {
                    var recencyFallback = await GetRecentIdeas(conversationId, limit: 5 - relevantIdeas.Count);
                    relevantIdeas = relevantIdeas.Concat(recencyFallback)
                        .DistinctBy(i => i.NodeId).ToList();
                }

                return payload with
                {
                    RelevantIdeas = relevantIdeas,
                    OpenThreads = threadsTask.Result,
                    RecentTurns = turnsTask.Result,
                    CrossProjectIdeas = crossTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    OpenQuestions = stats.OpenQuestions,
                    ParkedIdeas = stats.ParkedIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Deepening:
            {
                var crossTask = SemanticSearchOrFallback(userInput, conversationId, nodeTypes: null, limit: 3);

                // Tier 1: graph 2-hop for deep dive, or Tier 3: recency
                var graphTask = hasGraphSeeds
                    ? GetGraphContext(seedNodeIds, maxHops: 2, limit: 8)
                    : GetRecentIdeas(conversationId, limit: 3);

                await Task.WhenAll(statsTask, coverageTask, baselineTask, graphTask, crossTask);
                var stats = statsTask.Result;

                // Split graph results: direct neighbors → RelevantIdeas, 2-hop → ConnectedNodes
                var graphResults = graphTask.Result;
                var relevant = hasGraphSeeds
                    ? graphResults.Where(i => i.Relationship != null && !i.Relationship.Contains("hop:2")).ToList()
                    : graphResults;
                var connected = hasGraphSeeds
                    ? graphResults.Where(i => i.Relationship != null && i.Relationship.Contains("hop:2")).ToList()
                    : new List<ContextItem>();

                return payload with
                {
                    RelevantIdeas = relevant,
                    ConnectedNodes = connected,
                    CrossProjectIdeas = crossTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Recalling:
            {
                // Recalling is primarily semantic — graph supplements the top result
                var semanticTask = SemanticSearchOrFallback(userInput, conversationId, ideaTypes, limit: 5);
                await Task.WhenAll(statsTask, coverageTask, baselineTask, semanticTask);
                var stats = statsTask.Result;
                var semanticResults = semanticTask.Result;

                // Tier 1 supplement: graph neighbors of the top semantic result
                var connected = new List<ContextItem>();
                if (semanticResults.Count > 0 && _ageLayer != null)
                {
                    var topId = semanticResults[0].NodeId;
                    connected = await GetGraphContext(new[] { topId }, maxHops: 1, limit: 5);
                    // Exclude items already in semantic results
                    var existingIds = semanticResults.Select(s => s.NodeId).ToHashSet();
                    connected = connected.Where(c => !existingIds.Contains(c.NodeId)).ToList();
                }

                return payload with
                {
                    RelevantIdeas = semanticResults,
                    ConnectedNodes = connected,
                    TotalIdeas = stats.TotalIdeas,
                    OpenQuestions = stats.OpenQuestions,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Planning:
            {
                var threadsTask = GetOpenThreads(conversationId);
                var crossTask = SemanticSearchOrFallback(userInput, conversationId, nodeTypes: null, limit: 3);

                // Tier 1: graph with planning-relevant edge types
                var planEdges = new HashSet<string> { "IMPLEMENTED_BY", "BLOCKS", "CONSTRAINS", "SPAWNED_FROM", "RELATES_TO" };
                var graphTask = hasGraphSeeds
                    ? GetGraphContext(seedNodeIds, maxHops: 2, limit: 8, edgeTypeFilter: planEdges)
                    : GetRecentIdeas(conversationId, limit: 3);

                await Task.WhenAll(statsTask, coverageTask, baselineTask, graphTask, threadsTask, crossTask);
                var stats = statsTask.Result;

                return payload with
                {
                    RelevantIdeas = graphTask.Result,
                    OpenThreads = threadsTask.Result,
                    CrossProjectIdeas = crossTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    OpenQuestions = stats.OpenQuestions,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Parking:
            {
                var semanticTask = SemanticSearchOrFallback(userInput, conversationId, ideaTypes, limit: 5);
                await Task.WhenAll(statsTask, coverageTask, baselineTask, semanticTask);
                var stats = statsTask.Result;
                return payload with
                {
                    RelevantIdeas = semanticTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Resuming:
            {
                var semanticTask = SemanticSearchOrFallback(userInput, conversationId, ideaTypes, limit: 5);
                await Task.WhenAll(statsTask, coverageTask, baselineTask, semanticTask);
                var stats = statsTask.Result;
                return payload with
                {
                    RelevantIdeas = semanticTask.Result,
                    ParkedIdeas = stats.ParkedIdeas,
                    TotalIdeas = stats.TotalIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Reviewing:
            {
                var crossTask = SemanticSearchOrFallback(userInput, conversationId, nodeTypes: null, limit: 3);

                var graphTask = hasGraphSeeds
                    ? GetGraphContext(seedNodeIds, maxHops: 2, limit: 8)
                    : GetRecentIdeas(conversationId, limit: 5);

                await Task.WhenAll(statsTask, coverageTask, baselineTask, graphTask, crossTask);
                var stats = statsTask.Result;
                return payload with
                {
                    RelevantIdeas = graphTask.Result,
                    CrossProjectIdeas = crossTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            case Intent.Executing:
            {
                var turnsTask = GetRecentTurns(conversationId, limit: 3);
                var crossTask = SemanticSearchOrFallback(userInput, conversationId, nodeTypes: null, limit: 3);

                // Tier 1: graph with execution-relevant edge types
                var execEdges = new HashSet<string> { "IMPLEMENTED_BY", "PRODUCES", "CALLS", "REFERENCES", "RELATES_TO" };
                var graphTask = hasGraphSeeds
                    ? GetGraphContext(seedNodeIds, maxHops: 2, limit: 8, edgeTypeFilter: execEdges)
                    : GetRecentIdeas(conversationId, limit: 5);

                await Task.WhenAll(statsTask, coverageTask, baselineTask, graphTask, turnsTask, crossTask);
                var stats = statsTask.Result;
                return payload with
                {
                    RelevantIdeas = graphTask.Result,
                    RecentTurns = turnsTask.Result,
                    CrossProjectIdeas = crossTask.Result,
                    TotalIdeas = stats.TotalIdeas,
                    Coverage = coverageTask.Result,
                    NaiveBaselineChars = baselineTask.Result
                };
            }

            default:
                return payload;
        }
    }

    /// <summary>
    /// Get context items from graph neighbors of seed nodes.
    /// Converts AgeLayer.GetNeighborhood results into ContextItem records.
    /// </summary>
    private async Task<List<ContextItem>> GetGraphContext(
        IReadOnlyList<Guid> seedNodeIds, int maxHops = 2, int limit = 10,
        HashSet<string>? edgeTypeFilter = null)
    {
        if (_ageLayer == null || seedNodeIds.Count == 0)
            return new List<ContextItem>();

        var neighbors = await _ageLayer.GetNeighborhood(seedNodeIds, maxHops, edgeTypeFilter);
        return neighbors.Take(limit).Select(n => new ContextItem(
            n.NodeId,
            n.NodeType,
            n.Name,
            n.Value,
            null, // Status — would need an attribute lookup; skip for now
            null, // SimilarityScore — not applicable for graph neighbors
            $"{n.EdgeType} ({n.Direction}, hop:{n.Distance})"
        )).ToList();
    }

    /// <summary>Get recent ideas from a conversation's topics.</summary>
    private async Task<List<ContextItem>> GetRecentIdeas(Guid conversationId, int limit)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, n.value,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status
            FROM nodes n
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type IN ('idea', 'question', 'decision')
            ORDER BY n.modified_at DESC
            LIMIT @limit
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("limit", limit);

        return await ReadContextItems(cmd);
    }

    /// <summary>Get ideas with status 'mentioned' or 'explored' that haven't progressed.</summary>
    private async Task<List<ContextItem>> GetOpenThreads(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, n.value,
                   a.value as status
            FROM nodes n
            JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type = 'idea'
            AND a.value IN ('mentioned', 'explored')
            ORDER BY n.modified_at ASC
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);

        return await ReadContextItems(cmd);
    }

    /// <summary>Get the last N turns from a conversation (includes turns under threads).</summary>
    private async Task<List<ContextItem>> GetRecentTurns(Guid conversationId, int limit)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, n.value,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'speaker') as status
            FROM nodes n
            WHERE n.node_type = 'turn'
              AND (n.parent_id = @convId
                   OR n.parent_id IN (SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'))
            ORDER BY n.created_at DESC
            LIMIT @limit
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("limit", limit);

        var items = await ReadContextItems(cmd);
        items.Reverse(); // Chronological order
        return items;
    }

    /// <summary>
    /// Try semantic search first. If it returns empty (Ollama down or no embeddings),
    /// fall back to ILIKE text search scoped to the conversation.
    /// </summary>
    private async Task<List<ContextItem>> SemanticSearchOrFallback(
        string userInput, Guid conversationId, string[]? nodeTypes, int limit)
    {
        var results = await SemanticSearchNodes(userInput, nodeTypes, limit);
        if (results.Count > 0)
            return results;

        // Fallback: ILIKE search (conversation-scoped, less accurate but works without Ollama)
        return await SearchIdeasByText(conversationId, userInput);
    }

    /// <summary>
    /// Semantic search across ALL nodes (cross-project, cross-conversation).
    /// Uses pgvector cosine distance. Falls back to empty list if Ollama is unavailable.
    /// </summary>
    private async Task<List<ContextItem>> SemanticSearchNodes(string userInput, string[]? nodeTypes = null, int limit = 5)
    {
        if (_embeddingService == null)
            return new List<ContextItem>();

        var embedding = await _embeddingService.EmbedQueryAsync(userInput);
        if (embedding == null)
            return new List<ContextItem>(); // Ollama down — caller should fall back to ILIKE

        var vecStr = "[" + string.Join(",", embedding.Select(v => v.ToString("G"))) + "]";

        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Build optional node_type filter
        var typeFilter = "";
        var parameters = new List<NpgsqlParameter>();

        if (nodeTypes != null && nodeTypes.Length > 0)
        {
            var placeholders = string.Join(",", nodeTypes.Select((_, i) => $"@type{i}"));
            typeFilter = $"AND n.node_type IN ({placeholders})";
            for (int i = 0; i < nodeTypes.Length; i++)
                parameters.Add(new NpgsqlParameter($"type{i}", nodeTypes[i]));
        }

        await using var cmd = new NpgsqlCommand($"""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 500) as value,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status,
                   n.embedding <=> @vec::vector AS distance
            FROM nodes n
            WHERE n.embedding IS NOT NULL
            {typeFilter}
            ORDER BY n.embedding <=> @vec::vector
            LIMIT @limit
        """, conn);
        cmd.Parameters.AddWithValue("vec", vecStr);
        cmd.Parameters.AddWithValue("limit", limit);
        foreach (var p in parameters)
            cmd.Parameters.Add(p);

        var items = new List<ContextItem>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            items.Add(new ContextItem(
                NodeId: reader.GetGuid(0),
                NodeType: reader.GetString(1),
                Name: reader.IsDBNull(2) ? null : reader.GetString(2),
                Value: reader.IsDBNull(3) ? null : reader.GetString(3),
                Status: reader.IsDBNull(4) ? null : reader.GetString(4),
                SimilarityScore: reader.GetDouble(5),
                Relationship: null));
        }
        return items;
    }

    /// <summary>
    /// Semantic search for node references (lightweight — for AssembleForAgent).
    /// Falls back to empty list if Ollama is unavailable.
    /// </summary>
    private async Task<List<NodeRef>> SemanticSearchNodeRefs(string userInput, string[]? nodeTypes = null, int limit = 5)
    {
        var items = await SemanticSearchNodes(userInput, nodeTypes, limit);
        return items.Select(i => new NodeRef(
            NodeId: i.NodeId,
            NodeType: i.NodeType,
            Name: i.Name,
            Summary: i.Value?.Length > 80 ? i.Value[..80] : i.Value,
            Status: i.Status)).ToList();
    }

    /// <summary>
    /// Try semantic NodeRef search. If empty (Ollama down), fall back to ILIKE.
    /// </summary>
    private async Task<List<NodeRef>> SemanticSearchNodeRefsOrFallback(
        string userInput, Guid conversationId, string[]? nodeTypes, int limit = 5)
    {
        var results = await SemanticSearchNodeRefs(userInput, nodeTypes, limit);
        if (results.Count > 0)
            return results;

        // Fallback: ILIKE-based search (conversation-scoped)
        return await SearchNodeRefs(conversationId, userInput);
    }

    /// <summary>
    /// Get embedding coverage stats: how many nodes have embeddings vs total.
    /// Optional node_type filter.
    /// </summary>
    private async Task<SearchCoverage> GetSearchCoverage(string[]? nodeTypes = null)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        var typeFilter = "";
        var parameters = new List<NpgsqlParameter>();

        if (nodeTypes != null && nodeTypes.Length > 0)
        {
            var placeholders = string.Join(",", nodeTypes.Select((_, i) => $"@type{i}"));
            typeFilter = $"WHERE node_type IN ({placeholders})";
            for (int i = 0; i < nodeTypes.Length; i++)
                parameters.Add(new NpgsqlParameter($"type{i}", nodeTypes[i]));
        }

        await using var cmd = new NpgsqlCommand($"""
            SELECT
                COUNT(*) FILTER (WHERE embedding IS NOT NULL) as embedded,
                COUNT(*) as total
            FROM nodes
            {typeFilter}
        """, conn);
        foreach (var p in parameters)
            cmd.Parameters.Add(p);

        await using var reader = await cmd.ExecuteReaderAsync();
        await reader.ReadAsync();
        var embedded = reader.GetInt64(0);
        var total = reader.GetInt64(1);
        return new SearchCoverage(
            (int)embedded,
            (int)total,
            total > 0 ? Math.Round(embedded * 100.0 / total, 1) : 0);
    }

    /// <summary>Text-based idea search (substring match — ILIKE fallback when Ollama is down).</summary>
    private async Task<List<ContextItem>> SearchIdeasByText(Guid conversationId, string searchText)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        // Simple ILIKE search — in production, this would use pgvector semantic search
        var searchPattern = $"%{searchText.Replace("%", "").Replace("_", "")}%";

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, n.value,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status
            FROM nodes n
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type IN ('idea', 'question', 'decision')
            AND (n.name ILIKE @search OR n.value ILIKE @search)
            ORDER BY n.modified_at DESC
            LIMIT 5
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("search", searchPattern);

        return await ReadContextItems(cmd);
    }

    /// <summary>Get aggregate stats for a conversation.</summary>
    private async Task<ConversationStats> GetConversationStats(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT
                COUNT(*) FILTER (WHERE n.node_type = 'idea') as total_ideas,
                COUNT(*) FILTER (WHERE n.node_type = 'question'
                    AND EXISTS(SELECT 1 FROM node_attributes a
                               WHERE a.node_id = n.id AND a.key = 'status' AND a.value != 'resolved'))
                    as open_questions,
                COUNT(*) FILTER (WHERE n.node_type = 'idea'
                    AND EXISTS(SELECT 1 FROM node_attributes a
                               WHERE a.node_id = n.id AND a.key = 'status' AND a.value = 'parked'))
                    as parked_ideas
            FROM nodes n
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);

        await using var reader = await cmd.ExecuteReaderAsync();
        if (await reader.ReadAsync())
        {
            return new ConversationStats(
                reader.GetInt32(0),
                reader.GetInt32(1),
                reader.GetInt32(2));
        }
        return new ConversationStats(0, 0, 0);
    }

    /// <summary>
    /// Total chars of all turn values in the conversation — the naive baseline
    /// if we just dumped everything to the model instead of curating context.
    /// </summary>
    private async Task<int> GetTotalConversationChars(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT COALESCE(SUM(LENGTH(n.value)), 0)
            FROM nodes n
            WHERE n.node_type = 'turn'
              AND (n.parent_id = @convId
                   OR n.parent_id IN (
                       SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'thread'))
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);

        var result = await cmd.ExecuteScalarAsync();
        return Convert.ToInt32(result);
    }

    /// <summary>
    /// Assemble lightweight node references for an agent.
    /// The agent gets starting-point IDs and navigates the tree itself
    /// via GetSubtree/GetChildren/GetParent as needed.
    /// </summary>
    public async Task<AgentContext> AssembleForAgent(
        IntentResult intentResult,
        string userInput,
        Guid conversationId,
        string sourceAgent = "user",
        string targetAgent = "claude",
        string inputMode = "text")
    {
        var stats = await GetConversationStats(conversationId);
        var role = PromptBuilder.GetRoleDescriptionStatic(intentResult.Intent);

        // Get focus nodes — try semantic search first for search-based intents, fall back to ILIKE
        var ideaTypes = new[] { "idea", "question", "decision", "finding" };
        var focusNodes = intentResult.Intent switch
        {
            Intent.Ideation => await GetNodeRefs(conversationId, limit: 5),
            Intent.Deepening => await GetNodeRefs(conversationId, limit: 3),
            Intent.Planning => await GetNodeRefs(conversationId, limit: 3),
            Intent.Recalling => await SemanticSearchNodeRefsOrFallback(userInput, conversationId, ideaTypes),
            Intent.Parking => await SemanticSearchNodeRefsOrFallback(userInput, conversationId, ideaTypes),
            Intent.Resuming => await SemanticSearchNodeRefsOrFallback(userInput, conversationId, ideaTypes),
            Intent.Reviewing => await GetNodeRefs(conversationId, limit: 5),
            Intent.Executing => await GetNodeRefs(conversationId, limit: 3),
            _ => new List<NodeRef>()
        };

        var openThreads = (intentResult.Intent is Intent.Ideation or Intent.Planning)
            ? await GetOpenThreadRefs(conversationId)
            : new List<NodeRef>();

        return new AgentContext
        {
            Intent = intentResult.Intent,
            UserInput = userInput,
            InputMode = inputMode,
            SourceAgent = sourceAgent,
            TargetAgent = targetAgent,
            ConversationId = conversationId,
            FocusNodes = focusNodes,
            OpenThreads = openThreads,
            TotalIdeas = stats.TotalIdeas,
            OpenQuestions = stats.OpenQuestions,
            ParkedIdeas = stats.ParkedIdeas,
            RolePrompt = role
        };
    }

    /// <summary>Get node references (ID + summary, not full content).</summary>
    private async Task<List<NodeRef>> GetNodeRefs(Guid conversationId, int limit)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name,
                   LEFT(n.value, 80) as summary,
                   a.value as status
            FROM nodes n
            LEFT JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type IN ('idea', 'question', 'decision')
            ORDER BY n.modified_at DESC
            LIMIT @limit
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("limit", limit);

        return await ReadNodeRefs(cmd);
    }

    /// <summary>Get open thread node references.</summary>
    private async Task<List<NodeRef>> GetOpenThreadRefs(Guid conversationId)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name,
                   LEFT(n.value, 80) as summary,
                   a.value as status
            FROM nodes n
            JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type = 'idea'
            AND a.value IN ('mentioned', 'explored')
            ORDER BY n.modified_at ASC
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);

        return await ReadNodeRefs(cmd);
    }

    /// <summary>Search for node references by text match.</summary>
    private async Task<List<NodeRef>> SearchNodeRefs(Guid conversationId, string searchText)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        var searchPattern = $"%{searchText.Replace("%", "").Replace("_", "")}%";

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name,
                   LEFT(n.value, 80) as summary,
                   a.value as status
            FROM nodes n
            LEFT JOIN node_attributes a ON a.node_id = n.id AND a.key = 'status'
            WHERE n.parent_id IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
            )
            AND n.node_type IN ('idea', 'question', 'decision')
            AND (n.name ILIKE @search OR n.value ILIKE @search)
            ORDER BY n.modified_at DESC
            LIMIT 5
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("search", searchPattern);

        return await ReadNodeRefs(cmd);
    }

    private static async Task<List<NodeRef>> ReadNodeRefs(NpgsqlCommand cmd)
    {
        var refs = new List<NodeRef>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            refs.Add(new NodeRef(
                NodeId: reader.GetGuid(0),
                NodeType: reader.GetString(1),
                Name: reader.IsDBNull(2) ? null : reader.GetString(2),
                Summary: reader.IsDBNull(3) ? null : reader.GetString(3),
                Status: reader.IsDBNull(4) ? null : reader.GetString(4)));
        }
        return refs;
    }

    private static async Task<List<ContextItem>> ReadContextItems(NpgsqlCommand cmd)
    {
        var items = new List<ContextItem>();
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            items.Add(new ContextItem(
                NodeId: reader.GetGuid(0),
                NodeType: reader.GetString(1),
                Name: reader.IsDBNull(2) ? null : reader.GetString(2),
                Value: reader.IsDBNull(3) ? null : reader.GetString(3),
                Status: reader.IsDBNull(4) ? null : reader.GetString(4),
                SimilarityScore: null,
                Relationship: null));
        }
        return items;
    }

    /// <summary>
    /// Search ideas/decisions/questions across ALL projects, excluding the current conversation.
    /// This is how models see context from other sessions (e.g., Claude Code sessions show up
    /// in the chat UI, and vice versa).
    /// </summary>
    private async Task<List<ContextItem>> GetCrossProjectIdeas(Guid conversationId, string searchText, int limit = 3)
    {
        await using var conn = new NpgsqlConnection(_connStr);
        await conn.OpenAsync();

        var searchPattern = $"%{searchText.Replace("%", "").Replace("_", "")}%";

        await using var cmd = new NpgsqlCommand("""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
                   (SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status') as status
            FROM nodes n
            WHERE n.node_type IN ('idea', 'question', 'decision', 'turn')
            AND n.parent_id NOT IN (
                SELECT id FROM nodes WHERE parent_id = @convId AND node_type = 'topic'
                UNION ALL SELECT @convId
            )
            AND (n.name ILIKE @search OR n.value ILIKE @search)
            ORDER BY n.modified_at DESC
            LIMIT @limit
        """, conn);
        cmd.Parameters.AddWithValue("convId", conversationId);
        cmd.Parameters.AddWithValue("search", searchPattern);
        cmd.Parameters.AddWithValue("limit", limit);

        return await ReadContextItems(cmd);
    }

    private record ConversationStats(int TotalIdeas, int OpenQuestions, int ParkedIdeas);
}
