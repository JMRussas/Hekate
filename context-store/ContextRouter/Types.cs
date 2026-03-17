// CodeStoragePoc - Context Router Types
//
// Shared types for the context router: entity resolution, context payloads,
// agent contracts, and assembled prompts. Transport-agnostic.
//
// The core model: user input → resolve what they're talking about → assemble
// state around the resolved subject → define the agent's job via a contract.
// If resolution fails, ask for clarification instead of guessing.
//
// Depends on: (none)
// Used by:    InterpreterService, ContextAssembler, PromptBuilder, ChatService

namespace CodeStoragePoc.ContextRouter;

/// <summary>
/// What kind of request the user is making.
/// Determines which DB queries run and which model handles it.
/// </summary>
public enum Intent
{
    /// <summary>Exploring new ideas, brainstorming, open-ended thinking.</summary>
    Ideation,

    /// <summary>Drilling deeper into a specific idea or topic.</summary>
    Deepening,

    /// <summary>Turning an idea into actionable steps/plan.</summary>
    Planning,

    /// <summary>Evaluating prior work, getting a second opinion.</summary>
    Reviewing,

    /// <summary>Writing code, executing a plan step.</summary>
    Executing,

    /// <summary>"What did I say about X?" — retrieving prior ideas/decisions.</summary>
    Recalling,

    /// <summary>"Park that for now" — shelving an idea without losing it.</summary>
    Parking,

    /// <summary>"Let's revisit X" — pulling a parked/stale idea back.</summary>
    Resuming
}

/// <summary>
/// A relevant item surfaced from the database for context assembly.
/// Could be an idea, decision, question, code node, or plan step.
/// </summary>
public record ContextItem(
    Guid NodeId,
    string NodeType,
    string? Name,
    string? Value,
    string? Status,
    double? SimilarityScore,
    string? Relationship);

/// <summary>
/// The assembled context payload — everything a model needs to respond,
/// curated by intent rather than dumped as raw history.
/// </summary>
public record ContextPayload
{
    public Intent Intent { get; init; }
    public string UserInput { get; init; } = "";
    public string InputMode { get; init; } = "text";
    public Guid? ConversationId { get; init; }

    /// <summary>Ideas/decisions/questions semantically similar to the user's input.</summary>
    public List<ContextItem> RelevantIdeas { get; init; } = new();

    /// <summary>Graph neighbors — nodes connected to relevant ideas.</summary>
    public List<ContextItem> ConnectedNodes { get; init; } = new();

    /// <summary>Open threads — ideas stuck in mentioned/explored for too long.</summary>
    public List<ContextItem> OpenThreads { get; init; } = new();

    /// <summary>Recent turns — only the last few, not full history.</summary>
    public List<ContextItem> RecentTurns { get; init; } = new();

    /// <summary>Code context — code nodes relevant to the discussion.</summary>
    public List<ContextItem> CodeContext { get; init; } = new();

    /// <summary>Ideas/decisions from OTHER projects/conversations — cross-session memory.</summary>
    public List<ContextItem> CrossProjectIdeas { get; init; } = new();

    /// <summary>Summary stats for the session briefing.</summary>
    public int TotalIdeas { get; init; }
    public int OpenQuestions { get; init; }
    public int ParkedIdeas { get; init; }
    public int StaleThreadCount { get; init; }

    /// <summary>Embedding coverage — how many nodes have embeddings vs total.</summary>
    public SearchCoverage? Coverage { get; init; }

    /// <summary>Which model is responding (e.g., "sonnet", "haiku").</summary>
    public string? RespondingModel { get; init; }

    /// <summary>Permission level name (e.g., "Observe", "Suggest", "Assist", "Auto").</summary>
    public string? PermissionLabel { get; init; }

    /// <summary>Available skill names the model can call via tool-use.</summary>
    public List<string> AvailableSkills { get; init; } = new();

    /// <summary>Total chars of all turns in the conversation — the naive baseline
    /// if we just dumped everything to the model instead of curating context.</summary>
    public int NaiveBaselineChars { get; init; }

    /// <summary>Response formatting guidance from the interpreter (format, length, tone). Null when regex fallback.</summary>
    public ResponseGuidance? ResponseGuidance { get; init; }
}

/// <summary>
/// Response formatting guidance from the interpreter.
/// Tells the response model how to structure its answer.
/// </summary>
public record ResponseGuidance(
    string Format = "prose",
    string Length = "moderate",
    string Tone = "casual");

/// <summary>
/// Embedding coverage stats for the context store.
/// Shows what fraction of nodes are semantically searchable.
/// </summary>
public record SearchCoverage(int Embedded, int Total, double Pct);

/// <summary>
/// A node reference — just enough to decide whether to pull more detail.
/// Agents receive these and call GetSubtree/GetChildren/GetParent to expand.
/// </summary>
public record NodeRef(
    Guid NodeId,
    string NodeType,
    string? Name,
    string? Summary,
    string? Status);

/// <summary>
/// Lightweight context handoff to an agent. Contains node references,
/// not pre-fetched data. The agent navigates the tree itself.
/// </summary>
public record AgentContext
{
    public Intent Intent { get; init; }
    public string UserInput { get; init; } = "";
    public string InputMode { get; init; } = "text";
    public string SourceAgent { get; init; } = "user";
    public string TargetAgent { get; init; } = "claude";
    public Guid? ConversationId { get; init; }

    /// <summary>Starting-point nodes the agent should look at first.</summary>
    public List<NodeRef> FocusNodes { get; init; } = new();

    /// <summary>Nodes with open status that may need attention.</summary>
    public List<NodeRef> OpenThreads { get; init; } = new();

    /// <summary>Stats so the agent knows the landscape without querying.</summary>
    public int TotalIdeas { get; init; }
    public int OpenQuestions { get; init; }
    public int ParkedIdeas { get; init; }

    /// <summary>The role/instruction for this agent.</summary>
    public string RolePrompt { get; init; } = "";
}

/// <summary>
/// The Interpreter's structured output — replaces regex-based IntentResult.
/// Resolves: what project, what intent, what entities the user is referring to.
/// </summary>
public record InterpretedInput
{
    /// <summary>Project resolution: new, existing (with ID), or cross-project query.</summary>
    public ProjectResolution Project { get; init; } = new();

    /// <summary>Classified intent (same enum, but model-determined instead of regex).</summary>
    public Intent Intent { get; init; } = Intent.Ideation;

    /// <summary>Model's confidence in the classification (0.0 - 1.0).</summary>
    public double Confidence { get; init; }

    /// <summary>Entities resolved from the message (e.g., "the voice thing" → node ID).</summary>
    public List<ResolvedEntity> Entities { get; init; } = new();

    /// <summary>The cleaned user message (with @mentions stripped).</summary>
    public string CleanedMessage { get; init; } = "";

    /// <summary>Brief reasoning from the model about why it chose this interpretation.</summary>
    public string? Reasoning { get; init; }

    /// <summary>How long the interpretation took (ms).</summary>
    public long DurationMs { get; init; }

    /// <summary>Whether this fell back to regex (Ollama unavailable).</summary>
    public bool IsRegexFallback { get; init; }

    /// <summary>
    /// Response formatting guidance from the interpreter — tells the response model
    /// how to structure its answer (format, length, tone). Null when regex fallback.
    /// </summary>
    public ResponseGuidance? ResponseGuidance { get; init; }
}

/// <summary>
/// How the Interpreter resolved the project scope.
/// </summary>
public record ProjectResolution
{
    /// <summary>"new", "existing", or "cross_project"</summary>
    public string Type { get; init; } = "existing";

    /// <summary>Project ID (for existing) or null (for new/cross-project).</summary>
    public Guid? ProjectId { get; init; }

    /// <summary>Project name — either existing name or proposed name for new projects.</summary>
    public string? ProjectName { get; init; }
}

/// <summary>
/// An entity the user referenced, resolved to a node if possible.
/// </summary>
public record ResolvedEntity
{
    /// <summary>The mention text from the user's message (e.g., "the voice thing").</summary>
    public string Mention { get; init; } = "";

    /// <summary>Resolved node ID, if found via semantic search.</summary>
    public Guid? NodeId { get; init; }

    /// <summary>Resolved node type.</summary>
    public string? NodeType { get; init; }

    /// <summary>Resolved node name.</summary>
    public string? NodeName { get; init; }

    /// <summary>Similarity score from semantic search.</summary>
    public double? Score { get; init; }
}

/// <summary>
/// The final prompt ready to send to a model.
/// Contains the XML-structured context + role + user input.
/// </summary>
public record AssembledPrompt(
    Intent Intent,
    string SuggestedModel,
    string SystemPrompt,
    string UserPrompt);

// ============================================================================
// New types for entity-driven context assembly (Plan 010)
// ============================================================================

/// <summary>
/// The interpreter's output — what the user is talking about.
/// Either resolved nodes (success) or a clarification question (ambiguous).
/// This replaces intent classification as the driver for context assembly.
/// </summary>
public record ResolvedSubject
{
    /// <summary>Did resolution succeed? If false, ClarificationQuestion is populated.</summary>
    public bool IsResolved { get; init; }

    /// <summary>The resolved nodes — what the user is referring to.</summary>
    public List<ResolvedEntity> Entities { get; init; } = new();

    /// <summary>Node types of resolved entities — drives context assembly.
    /// e.g., plan nodes → pull plan state, idea nodes → pull idea subtree.</summary>
    public HashSet<string> SubjectTypes { get; init; } = new();

    /// <summary>Confidence in the resolution (0.0 - 1.0).</summary>
    public double Confidence { get; init; }

    /// <summary>If not resolved: what to ask the user.</summary>
    public string? ClarificationQuestion { get; init; }

    /// <summary>Candidate matches if ambiguous — user can pick one.</summary>
    public List<ResolvedEntity> CandidateMatches { get; init; } = new();

    /// <summary>Project scope.</summary>
    public ProjectResolution Project { get; init; } = new();

    /// <summary>Whether this is a direct action (park/resume) vs needs model response.</summary>
    public bool IsDirectAction { get; init; }

    /// <summary>If direct action: "park" or "resume".</summary>
    public string? DirectActionType { get; init; }

    /// <summary>Response formatting guidance.</summary>
    public ResponseGuidance? ResponseGuidance { get; init; }

    /// <summary>The cleaned user message.</summary>
    public string CleanedMessage { get; init; } = "";

    /// <summary>How long resolution took (ms).</summary>
    public long DurationMs { get; init; }

    /// <summary>Whether this used regex fallback.</summary>
    public bool IsRegexFallback { get; init; }

    /// <summary>Brief reasoning from the resolver.</summary>
    public string? Reasoning { get; init; }

    /// <summary>Derive a display intent from subject types (for SSE compat).
    /// Does NOT drive context assembly — that's determined by SubjectTypes.</summary>
    public Intent DisplayIntent
    {
        get
        {
            if (IsDirectAction)
                return DirectActionType == "park" ? Intent.Parking : Intent.Resuming;

            if (SubjectTypes.Count == 0)
                return Intent.Ideation; // New topic, no resolved entities

            // Plan domain
            if (SubjectTypes.Any(t => t is "plan" or "plan_step" or "plan_phase" or "task" or "milestone"))
                return Intent.Planning;

            // Code domain
            if (SubjectTypes.Any(t => t is "method" or "class" or "struct" or "compilation_unit" or "namespace" or "field"))
                return Intent.Executing;

            // Research domain
            if (SubjectTypes.Any(t => t is "research" or "finding" or "open_question" or "reference"))
                return Intent.Reviewing;

            // Idea domain
            if (SubjectTypes.Any(t => t is "idea" or "question" or "decision" or "action_item"))
                return Intent.Deepening;

            // Conversation/thread — likely recalling
            if (SubjectTypes.Any(t => t is "conversation" or "thread" or "turn"))
                return Intent.Recalling;

            return Intent.Ideation;
        }
    }
}

/// <summary>
/// Defines what an agent receives, produces, and does.
/// Replaces prose role prompts with a structured contract.
/// </summary>
public record AgentContract
{
    /// <summary>One-sentence description of what this agent does.</summary>
    public string Function { get; init; } = "";

    /// <summary>What the agent receives as input — the resolved subject + state.</summary>
    public string InputDescription { get; init; } = "";

    /// <summary>What the agent should produce.</summary>
    public string OutputDescription { get; init; } = "";

    /// <summary>Things the agent should NOT do.</summary>
    public List<string> Constraints { get; init; } = new();
}

/// <summary>
/// State assembled around a resolved subject — what the model needs to know.
/// Replaces intent-driven context with subject-driven state.
/// </summary>
public record SubjectState
{
    /// <summary>The resolved subject this state is about.</summary>
    public ResolvedSubject Subject { get; init; } = new();

    /// <summary>Full state of the resolved nodes — attributes, status, values.</summary>
    public List<SubjectNodeState> ResolvedNodeStates { get; init; } = new();

    /// <summary>Graph neighbors — structurally connected via edges.</summary>
    public List<ContextItem> ConnectedNodes { get; init; } = new();

    /// <summary>Semantically related nodes (cross-project, cross-conversation).</summary>
    public List<ContextItem> RelatedNodes { get; init; } = new();

    /// <summary>Open blockers/questions relevant to the subject.</summary>
    public List<ContextItem> OpenItems { get; init; } = new();

    /// <summary>Session-level stats.</summary>
    public int TotalIdeas { get; init; }
    public int OpenQuestions { get; init; }
    public int ParkedIdeas { get; init; }

    /// <summary>Embedding coverage.</summary>
    public SearchCoverage? Coverage { get; init; }

    /// <summary>Which model is responding.</summary>
    public string? RespondingModel { get; init; }

    /// <summary>Permission level.</summary>
    public string? PermissionLabel { get; init; }

    /// <summary>Available skills.</summary>
    public List<string> AvailableSkills { get; init; } = new();

    /// <summary>Recent conversation turns — last few messages for short-reply context.</summary>
    public List<ContextItem> RecentTurns { get; init; } = new();

    /// <summary>Response formatting guidance.</summary>
    public ResponseGuidance? ResponseGuidance { get; init; }

    /// <summary>The agent's job contract.</summary>
    public AgentContract Contract { get; init; } = new();
}

/// <summary>
/// Full state of a single resolved node — its attributes and children summary.
/// </summary>
public record SubjectNodeState
{
    public Guid NodeId { get; init; }
    public string NodeType { get; init; } = "";
    public string? Name { get; init; }
    public string? Value { get; init; }
    public string? Status { get; init; }
    public Dictionary<string, string> Attributes { get; init; } = new();
    public int ChildCount { get; init; }
    public List<string> ChildTypes { get; init; } = new();
}
