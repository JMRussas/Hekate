// CodeStoragePoc - Conversation Seeder
//
// Seeds a sample conversation with turns, topics, ideas, questions, and decisions.
// Demonstrates the transport-agnostic node model — turns are tagged with input_mode
// (voice or text) but the structure is identical downstream.
//
// Node hierarchy:
//   conversation (root per session)
//   ├── turn (user/model utterances in order)
//   ├── topic (groups of related ideas, created by extraction)
//   │   ├── idea (extracted from turns)
//   │   ├── question (unresolved questions)
//   │   └── decision (decisions made)
//   └── action_item (things to do)
//
// Graph edges (created separately):
//   turn ──EXTRACTED──→ idea/question/decision
//   idea ──RELATES_TO──→ idea
//   question ──BLOCKS──→ decision
//
// Depends on: NodeRepository, AgeLayer
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class ConversationSeeder
{
    private readonly NodeRepository _repo;

    public Guid ConversationId { get; private set; }
    public Guid TopicId { get; private set; }

    // Expose turn/idea IDs for graph edge creation
    public Guid Turn1Id { get; private set; }
    public Guid Turn2Id { get; private set; }
    public Guid Turn3Id { get; private set; }
    public Guid Turn4Id { get; private set; }
    public Guid Turn5Id { get; private set; }
    public Guid Turn6Id { get; private set; }

    public Guid Idea1Id { get; private set; }
    public Guid Idea2Id { get; private set; }
    public Guid Question1Id { get; private set; }
    public Guid Decision1Id { get; private set; }
    public Guid ActionItem1Id { get; private set; }

    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public ConversationSeeder(NodeRepository repo) => _repo = repo;

    /// <summary>Seed a sample conversation. Returns the root conversation node ID.</summary>
    public async Task<Guid> Seed(Guid projectId)
    {
        ConversationId = Guid.NewGuid();

        await _repo.InsertNode(
            ConversationId, projectId, null,
            "conversation", "Ideation Session 2026-03-08", null,
            null, 0, "system",
            new()
            {
                { "started_at", "2026-03-08T14:30:00Z" },
                { "session_type", "ideation" }
            });
        _allNodeIds.Add(ConversationId);

        // --- Turns (the timeline of the conversation) ---

        // User asks a question → targets any available agent
        Turn1Id = await SeedTurn(projectId, ConversationId, 100,
            "user", "text",
            "What if we used event sourcing instead of CRUD for the orchestration engine?",
            target: "agent");

        // Claude responds to user
        Turn2Id = await SeedTurn(projectId, ConversationId, 200,
            "claude", "text",
            "Event sourcing would give you a complete audit trail of every state change. " +
            "The orchestration engine already tracks task transitions — event sourcing " +
            "would make that explicit. The tradeoff is query complexity: you'd need " +
            "projections for current state, which adds infrastructure.",
            target: "user");

        // User speaks (voice) → targets any agent
        Turn3Id = await SeedTurn(projectId, ConversationId, 300,
            "user", "voice",
            "Yeah and maybe we could use Kafka for the event store. " +
            "We already have Docker running, adding Kafka isn't hard.",
            target: "agent");

        // Gemini responds via voice
        Turn4Id = await SeedTurn(projectId, ConversationId, 400,
            "gemini", "voice",
            "Kafka works as an event log but it's not a database — you can't query " +
            "events by aggregate ID efficiently. EventStoreDB is purpose-built for " +
            "event sourcing. Or you could use your existing PostgreSQL with an events " +
            "table — simplest option given your stack.",
            target: "user");

        // User asks follow-up → targets claude specifically
        Turn5Id = await SeedTurn(projectId, ConversationId, 500,
            "user", "text",
            "Good point about Postgres. What about CQRS? Do we need the full pattern?",
            target: "agent", targetAgent: "claude");

        // Claude responds to user
        Turn6Id = await SeedTurn(projectId, ConversationId, 600,
            "claude", "text",
            "For the orchestration engine, you probably don't need full CQRS. " +
            "The read and write models aren't different enough to justify the " +
            "complexity. A simpler approach: append events to a table, project " +
            "current state into a materialized view. You get the audit trail " +
            "without the architectural overhead.",
            target: "user");

        // --- Topic (groups related ideas) ---

        TopicId = Guid.NewGuid();
        await _repo.InsertNode(
            TopicId, projectId, null,
            "topic", "Event Sourcing for Orchestration", null,
            ConversationId, 1000, "claude",
            new() { { "status", "explored" } });
        _allNodeIds.Add(TopicId);

        // --- Extracted ideas, questions, decisions ---

        Idea1Id = await SeedIdea(projectId, TopicId, 100,
            "Event sourcing for orchestration engine",
            "Replace CRUD with event-append pattern for task state transitions",
            "explored");

        Idea2Id = await SeedIdea(projectId, TopicId, 200,
            "Use PostgreSQL events table instead of dedicated event store",
            "Append events to Postgres table, project via materialized view",
            "mentioned");

        Question1Id = Guid.NewGuid();
        await _repo.InsertNode(
            Question1Id, projectId, null,
            "question", null,
            "Do we need full CQRS or is a simpler event-append + materialized view enough?",
            TopicId, 300, "claude",
            new()
            {
                { "status", "resolved" },
                { "resolution", "Simpler approach — events table + materialized view. Full CQRS is overkill." }
            });
        _allNodeIds.Add(Question1Id);

        Decision1Id = Guid.NewGuid();
        await _repo.InsertNode(
            Decision1Id, projectId, null,
            "decision", "Skip full CQRS",
            "Use event-append to Postgres with materialized view for current state. " +
            "Gets audit trail without architectural overhead.",
            TopicId, 400, "claude",
            new() { { "status", "committed" } });
        _allNodeIds.Add(Decision1Id);

        // --- Action item ---

        ActionItem1Id = Guid.NewGuid();
        await _repo.InsertNode(
            ActionItem1Id, projectId, null,
            "action_item", "Design events table schema",
            "Define the events table: aggregate_id, event_type, payload (JSONB), " +
            "timestamp, sequence_number. Create materialized view for current task state.",
            ConversationId, 1100, "claude",
            new() { { "status", "pending" } });
        _allNodeIds.Add(ActionItem1Id);

        Console.WriteLine($"[SEED]     Conversation seeded: {_allNodeIds.Count} nodes");
        return ConversationId;
    }

    /// <summary>Create graph edges between turns and their extracted ideas.</summary>
    public async Task SeedEdges(GraphLayer.AgeLayer age)
    {
        // Turns → extracted ideas/questions/decisions
        await age.CreateEdge(Turn2Id, Idea1Id, "EXTRACTED");
        await age.CreateEdge(Turn4Id, Idea2Id, "EXTRACTED");
        await age.CreateEdge(Turn6Id, Question1Id, "EXTRACTED");
        await age.CreateEdge(Turn6Id, Decision1Id, "EXTRACTED");

        // Idea relationships
        await age.CreateEdge(Idea2Id, Idea1Id, "RELATES_TO");

        // Question blocks decision (resolved)
        await age.CreateEdge(Question1Id, Decision1Id, "BLOCKS");

        Console.WriteLine("[SEED]     Conversation edges: EXTRACTED(4) RELATES_TO(1) BLOCKS(1)");
    }

    private async Task<Guid> SeedTurn(Guid projectId, Guid parentId, int order,
        string speaker, string inputMode, string text,
        string target = "user", string? targetAgent = null)
    {
        var id = Guid.NewGuid();
        var attrs = new Dictionary<string, string>
        {
            { "speaker", speaker },
            { "input_mode", inputMode },
            { "target", target }
        };
        if (targetAgent != null)
            attrs["target_agent"] = targetAgent;

        await _repo.InsertNode(
            id, projectId, null,
            "turn", null, text,
            parentId, order, speaker,
            attrs);
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedIdea(Guid projectId, Guid parentId, int order,
        string name, string description, string status)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "idea", name, description,
            parentId, order, "claude",
            new() { { "status", status } });
        _allNodeIds.Add(id);
        return id;
    }
}
