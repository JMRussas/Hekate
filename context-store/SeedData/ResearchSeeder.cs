// CodeStoragePoc - Research Seeder
//
// Seeds all research documents (001-006) as node trees in PostgreSQL.
// Each research doc becomes a subtree: research root → findings, open_questions,
// decisions, references. Graph edges connect research to related plans.
//
// Node hierarchy:
//   research (root per doc)
//   ├── finding (key insight or conclusion)
//   ├── open_question (unresolved question)
//   ├── decision (committed design choice)
//   └── reference (link to external work)
//
// Graph edges (created separately):
//   research ──INFORMS──→ plan
//   research ──RELATES_TO──→ research
//   finding ──IMPLEMENTED_BY──→ code node (future)
//
// Depends on: NodeRepository, AgeLayer
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.SeedData;

public class ResearchSeeder
{
    private readonly NodeRepository _repo;

    // Root IDs for graph edge creation
    public Guid Research001Id { get; private set; }
    public Guid Research002Id { get; private set; }
    public Guid Research003Id { get; private set; }
    public Guid Research004Id { get; private set; }
    public Guid Research005Id { get; private set; }
    public Guid Research006Id { get; private set; }
    public Guid RoadmapId { get; private set; }

    private readonly List<Guid> _allNodeIds = new();
    public IReadOnlyList<Guid> AllNodeIds => _allNodeIds;

    public ResearchSeeder(NodeRepository repo) => _repo = repo;

    /// <summary>Seed all research docs and roadmap root. Returns the roadmap node ID.</summary>
    public async Task<Guid> Seed(Guid projectId)
    {
        // --- Roadmap root ---
        RoadmapId = Guid.NewGuid();
        await _repo.InsertNode(
            RoadmapId, projectId, null,
            "roadmap", "CodeStoragePoc Roadmap", "All plans, research, and ideas for the DB-as-source-of-truth project",
            null, 0, "claude-opus-4-6",
            new()
            {
                { "status", "active" },
                { "created_date", "2026-03-09" }
            });
        _allNodeIds.Add(RoadmapId);

        // --- Research 001: Voice AI Stack ---
        Research001Id = await SeedResearch(projectId, RoadmapId, 100,
            "001", "Voice AI Stack Research",
            "STT, TTS, voice framework comparisons for the ideation assistant",
            "completed", "2026-03-08");

        await SeedFinding(projectId, Research001Id, 100,
            "Local STT: faster-whisper on RTX 4090",
            "faster-whisper provides zero-cost, low-latency speech-to-text locally on RTX 4090");
        await SeedFinding(projectId, Research001Id, 200,
            "Local TTS: Kokoro-82M",
            "Kokoro-82M runs locally, produces natural speech, no API cost");
        await SeedFinding(projectId, Research001Id, 300,
            "Voice conversations: Gemini Live",
            "Gemini Live provides native voice conversation capability via subscription");
        await SeedDecision(projectId, Research001Id, 100,
            "Transport-agnostic node model",
            "Voice and text input produce identical node structures — only input_mode attribute differs",
            "committed");

        // --- Research 002: Architecture Review ---
        Research002Id = await SeedResearch(projectId, RoadmapId, 200,
            "002", "Architecture Review Findings",
            "Claude Opus + Gemini review of CodeStoragePoc — identifies 8 production blockers",
            "superseded", "2026-03-08");

        await SeedFinding(projectId, Research002Id, 100,
            "Unified node model is sound",
            "The single-table node model with types as strings is architecturally valid");
        await SeedFinding(projectId, Research002Id, 200,
            "Context router is a key asset",
            "Intent-driven context assembly is the right pattern for multi-model systems");
        await SeedFinding(projectId, Research002Id, 300,
            "8 production blockers identified",
            "N+1 queries, intent brittleness, no versioning, graph sync, embedding refresh, and more");
        await SeedReference(projectId, Research002Id, 100,
            "Superseded by research 003",
            "003-consolidated-review.md extends and replaces this review");

        // --- Research 003: Consolidated Review ---
        Research003Id = await SeedResearch(projectId, RoadmapId, 300,
            "003", "Consolidated Code Review",
            "Extended review combining 002 + deeper analysis. 7 critical/high bugs found and fixed via Plan 003",
            "completed", "2026-03-08");

        await SeedFinding(projectId, Research003Id, 100,
            "Advisory lock session mismatch",
            "NodeRepository was opening new connections instead of using the lock session — locks were broken");
        await SeedFinding(projectId, Research003Id, 200,
            "Cypher injection vulnerability",
            "Edge type and values were concatenated into Cypher without sanitization");
        await SeedFinding(projectId, Research003Id, 300,
            "Lock release ordering bug",
            "Locks were released before validation completed, creating race conditions");
        await SeedFinding(projectId, Research003Id, 400,
            "No transaction boundaries",
            "InsertNode was non-atomic — node and attributes could partially fail");
        await SeedDecision(projectId, Research003Id, 100,
            "All critical bugs fixed in Plan 003",
            "Advisory locks, Cypher injection, lock ordering, hardcoded creds, transactions, sequential assembly — all fixed",
            "committed");

        // --- Research 004: Temporal Graph Version Control ---
        Research004Id = await SeedResearch(projectId, RoadmapId, 400,
            "004", "Temporal Graph Version Control",
            "Concept: time-bounded node states instead of git snapshots. valid_from/valid_to on nodes, commits group mutations",
            "concept", "2026-03-08");

        await SeedFinding(projectId, Research004Id, 100,
            "Temporal edges enable git-like semantics",
            "valid_from/valid_to on nodes gives time-travel queries, structural diffs, three-way merges");
        await SeedFinding(projectId, Research004Id, 200,
            "Branches as zero-cost graph pointers",
            "No file copying — branches are just pointers into the temporal graph");
        await SeedOpenQuestion(projectId, Research004Id, 100,
            "Performance of temporal queries at scale",
            "Need benchmarks: temporal range queries on 100K+ nodes with GiST indexes");
        await SeedReference(projectId, Research004Id, 100,
            "db-ast-poc implementing temporal edges",
            "~/Git/db-ast-poc already has valid_from/valid_to/provenance on edges");

        // --- Research 005: Test Plans as Nodes ---
        Research005Id = await SeedResearch(projectId, RoadmapId, 500,
            "005", "Test Plans as Nodes",
            "Maps test suites/cases/steps as node types. Edges: VERIFIES, VERIFIED_BY, REQUIRES, GUARDS",
            "concept", "2026-03-09");

        await SeedFinding(projectId, Research005Id, 100,
            "Test specs become requirements nodes",
            "test_spec nodes are requirements; test_case nodes are implementations. Query coverage via edges");
        await SeedFinding(projectId, Research005Id, 200,
            "Graph queries for test coverage",
            "What tests cover this code? What specs lack test implementation? All Cypher queries");
        await SeedOpenQuestion(projectId, Research005Id, 100,
            "How to auto-generate VERIFIES edges",
            "Manual linking is fragile. Could analyze test assertions to infer which code nodes they verify");

        // --- Research 006: Agent Executable Context ---
        Research006Id = await SeedResearch(projectId, RoadmapId, 600,
            "006", "Agent Executable Context",
            "Database as runtime: agents write, compose, and execute code on demand via MCP. " +
            "Three shifts: execution as tool, agents create own tools, context is a query not a file",
            "active", "2026-03-09");

        await SeedFinding(projectId, Research006Id, 100,
            "Code execution as an MCP tool",
            "Agents get an execute tool: materialize nodes → Roslyn compile → run → return stdout/stderr");
        await SeedFinding(projectId, Research006Id, 200,
            "Agents create their own tools",
            "Agent writes code, executes it, tags as reusable. System accumulates capability via pgvector search");
        await SeedFinding(projectId, Research006Id, 300,
            "Context is a query, not a file",
            "Compilation unit = any set of nodes assembled by query. GenerateFromNodes(nodeSet) replaces GenerateFromFile(fileId)");
        await SeedFinding(projectId, Research006Id, 400,
            "Replayable agent conversations",
            "Every agent action is a node with cause→effect edges. Enables replay, fork, cross-agent comparison, debugging");
        await SeedFinding(projectId, Research006Id, 500,
            "Self-hosted roadmap",
            "The system manages its own development. Research, plans, ideas, conversations — all nodes, all visualizable");

        await SeedOpenQuestion(projectId, Research006Id, 100,
            "How does the agent specify what to execute?",
            "Entry point options: method name + args, expression, test assertion");
        await SeedOpenQuestion(projectId, Research006Id, 200,
            "How are NuGet dependencies handled?",
            "Pre-compiled reference assemblies or on-demand restore");
        await SeedOpenQuestion(projectId, Research006Id, 300,
            "State between executions — fresh or persistent?",
            "Each run starts fresh vs agent builds up state across calls");
        await SeedOpenQuestion(projectId, Research006Id, 400,
            "Multi-language support?",
            "Node model is language-agnostic. Could add Python/JS materializers");
        await SeedOpenQuestion(projectId, Research006Id, 500,
            "How do agents discover previously-created tools?",
            "pgvector search by description, or a dedicated agent_tool node type");

        await SeedDecision(projectId, Research006Id, 100,
            "In-memory Roslyn compilation for execution engine",
            "CSharpCompilation → AssemblyLoadContext → reflection. Temp project fallback for NuGet deps",
            "proposed");

        await SeedReference(projectId, Research006Id, 100,
            "Context router as precedent",
            "ContextAssembler already assembles dynamic context from DB queries — same pattern for executable code");
        await SeedReference(projectId, Research006Id, 200,
            "db-ast-poc temporal edges for audit trail",
            "valid_from/valid_to edges provide full history of agent mutations");

        Console.WriteLine($"[SEED]     Research seeded: {_allNodeIds.Count} nodes (6 docs + roadmap root)");
        return RoadmapId;
    }

    /// <summary>Create graph edges linking research to plans and to each other.</summary>
    public async Task SeedEdges(
        GraphLayer.AgeLayer age,
        Guid? plan001Id = null, Guid? plan002Id = null, Guid? plan003Id = null,
        Guid? plan004Id = null, Guid? plan005Id = null)
    {
        // Research → Plan (INFORMS)
        if (plan001Id.HasValue)
        {
            await age.CreateEdge(Research001Id, plan001Id.Value, "INFORMS");
            await age.CreateEdge(Research006Id, plan001Id.Value, "RELATES_TO");
        }
        if (plan002Id.HasValue)
            await age.CreateEdge(Research002Id, plan002Id.Value, "INFORMS");
        if (plan003Id.HasValue)
        {
            await age.CreateEdge(Research003Id, plan003Id.Value, "INFORMS");
            await age.CreateEdge(Research002Id, plan003Id.Value, "INFORMS");
        }

        // Research → Research (RELATES_TO)
        await age.CreateEdge(Research002Id, Research003Id, "RELATES_TO");  // 002 superseded by 003
        await age.CreateEdge(Research004Id, Research006Id, "RELATES_TO");  // temporal edges relate to agent context
        await age.CreateEdge(Research005Id, Research003Id, "RELATES_TO");  // test plans relate to review fixes

        Console.WriteLine("[SEED]     Research edges created");
    }

    // --- Helper methods (match PlanSeeder pattern) ---

    private async Task<Guid> SeedResearch(Guid projectId, Guid parentId, int order,
        string docNumber, string name, string description, string status, string createdDate)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "research", name, description,
            parentId, order, "claude-opus-4-6",
            new()
            {
                { "doc_number", docNumber },
                { "status", status },
                { "created_date", createdDate },
                { "source_file", $"research/{docNumber}-*.md" }
            });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedFinding(Guid projectId, Guid parentId, int order,
        string name, string description)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "finding", name, description,
            parentId, order, "claude-opus-4-6");
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedOpenQuestion(Guid projectId, Guid parentId, int order,
        string questionText, string proposedAnswer)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "open_question", null, questionText,
            parentId, order, "claude-opus-4-6",
            new()
            {
                { "proposed_answer", proposedAnswer },
                { "status", "open" }
            });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedDecision(Guid projectId, Guid parentId, int order,
        string name, string description, string status)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "decision", name, description,
            parentId, order, "claude-opus-4-6",
            new() { { "status", status } });
        _allNodeIds.Add(id);
        return id;
    }

    private async Task<Guid> SeedReference(Guid projectId, Guid parentId, int order,
        string name, string description)
    {
        var id = Guid.NewGuid();
        await _repo.InsertNode(
            id, projectId, null,
            "reference", name, description,
            parentId, order, "claude-opus-4-6");
        _allNodeIds.Add(id);
        return id;
    }
}
