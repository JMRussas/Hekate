# 009 — Capabilities Assessment

What the agent context store can actually do today, as built. This is not the vision (see 007) — it's the current state of working functionality.

## Core Capabilities

### 1. Persistent, Shared Agent Memory

Any MCP-capable agent (Claude Code, Gemini, Codex) stores and retrieves decisions, findings, ideas, and plans via the `agent-context` MCP server. Knowledge accumulates across sessions and across agents. Agents stop being stateless.

**What's working:**
- `store_node` persists any structured knowledge with auto-embedding (nomic-embed-text, 768-dim)
- `get_node` / `get_children` for tree navigation
- `query_nodes` for text search (ILIKE substring)
- `semantic_search` for meaning-based retrieval (pgvector cosine similarity)
- `history` for temporal edge traversal
- `list_projects` / `list_recent` for discovery
- `ensure_project` for stable project IDs

**What this replaces:** Flat MEMORY.md files that aren't queryable, aren't shared between agents, and truncate at 200 lines.

### 2. Semantic Search Across Everything

pgvector + Ollama nomic-embed-text enables conceptual queries across the entire database — cross-project, cross-conversation, cross-domain.

**What's working:**
- Every node stored via MCP or the C# API gets auto-embedded (fire-and-forget)
- `semantic_search` returns ranked results with similarity scores
- Coverage metric reports embedded/total/percentage
- ILIKE fallback when Ollama is unavailable
- `backfill_embeddings.py` for retroactive embedding of existing nodes
- Current coverage: 94% (213/226 nodes as of Plan 008 completion)

**What this replaces:** Keyword grep across files. An agent asking "how does auth work?" gets semantically relevant nodes, not string matches on the word "auth."

### 3. Entity-Driven Context Assembly

The context router resolves *what the user is talking about*, then assembles just the relevant state. Models receive curated subject state, not raw chat history.

**What's working:**
- `InterpreterService.ResolveAsync()` — Claude Haiku resolves user references ("the voice thing", "that plan") to existing node IDs
- Clarification path — if ambiguous, returns a question instead of guessing
- `ContextAssembler.AssembleFromSubject()` — resolved entity types determine what to pull:
  - `ResolvedNodeStates`: full state of each node (attributes, child count, child types)
  - `ConnectedNodes`: graph neighbors via AGE, filtered by domain-specific edge types
  - `RelatedNodes`: semantically similar nodes via pgvector
  - `OpenItems`: blockers, risks, open questions relevant to the subject
- `PromptBuilder.BuildFromSubjectState()` — agent contract + subject state → model-ready XML prompt
- Batched queries: 3 total for N resolved nodes (not N×3)

**What this replaces:** Stuffing the last N messages into the prompt. The model gets the *state of things*, not a replay of how we got there.

### 4. Multi-Domain Knowledge Graph

Code, plans, ideas, conversations, research, decisions — all nodes in the same Postgres table, connected via AGE Cypher edges. The cross-domain connections are the product.

**What's working:**
- 20+ node types across 6 domains (code, planning, ideation, tool-use, research, agent action)
- 15+ edge labels with domain-specific semantics
- AGE Cypher queries for graph traversal (`CALLS`, `REFERENCES`, `IMPLEMENTED_BY`, `SPAWNED_FROM`, etc.)
- Vertex sync from relational nodes to AGE graph
- Cross-project edges (plan tasks in one project linked to code nodes in another)

**Example queries the graph answers:**
- "What ideas became plans that became code?" — follow `SPAWNED_FROM` → `IMPLEMENTED_BY`
- "What does this method call?" — follow `CALLS` edges
- "What decisions constrain this plan step?" — follow `CONSTRAINS` edges
- "What research informed this decision?" — follow `INFORMS` edges

### 5. Temporal Version Control

Temporal edges track relationship history with `valid_from`, `valid_to`, and `provenance` properties. Version control at a granularity finer than Git commits.

**What's working:**
- `CreateTemporalEdge` in AgeLayer with timestamp properties
- `CloseTemporalEdge` sets `valid_to` when relationships are superseded
- `GetTemporalHistory` queries full edge history for a node
- Plan-to-code regeneration closes old edges and creates new ones
- `history` MCP tool exposes temporal edges to external agents
- AGE correctly treats missing `valid_to` as NULL (current/active)

**What this replaces:** Git log for relationship history. "Which version of the plan produced which version of the code?" is a graph query, not a `git blame`.

### 6. Full Code Round-Trip

C# source → Roslyn decomposition → structured node tree → materialization back to compilable C#. Plan tasks with structured attributes can drive code generation.

**What's working:**
- `CSharpDecomposer` — Roslyn parser → deep node tree (compilation_unit → namespace → class → method → parameter + block → statement)
- `EdgeSeeder` — scans statement bodies for field/method references → CALLS/REFERENCES edges
- `CSharpGenerator` — node tree → valid C# source
- `RoslynValidator` — parse-level validation before write
- `DotnetBuilder` — dotnet build wrapper with concurrent stdout/stderr
- `PlanToCodeGenerator` — plan tasks with `method_name`, `return_type`, `params`, `body` attributes → code nodes + `IMPLEMENTED_BY` temporal edges
- Mutation cycle: modify plan → close old edges → regenerate code → rebuild — proven working

**API endpoints:**
- `POST /api/code/decompose` — source → nodes
- `POST /api/code/materialize` — nodes → source
- `GET /api/code/files/{projectId}` — tracked files with node counts

### 7. Multi-Agent Orchestration

Multiple AI models can work concurrently on the same conversation, with tool-use, permissions, and event-driven triggers.

**What's working:**
- **Inter-model routing**: `@haiku`, `@sonnet` mention syntax routes to different models within the same conversation
- **Concurrent SSE streams**: ChatPanel supports multiple simultaneous model responses, each with independent phase tracking
- **Tool-use loop**: up to 5 iterations per response (model calls tool → execute → send result → repeat)
- **Built-in skills**: `route_to_model`, `search_ideas`, `get_node_details`, `list_threads`
- **Hot-reloaded skill config**: `tools/skills/skills.json` reloaded on every request
- **Permission system**: 4 levels (Observe/Suggest/Assist/Auto) per conversation, with per-model overrides
- **Approval workflow**: actions above permission level create `pending_action` nodes, surfaced as approval cards in the UI
- **Agent dispatcher**: `pg_notify` → trigger rule matching → CLI agent spawn → result pushed to chat via SystemMessageBus
- **Debouncing**: 30-second window prevents cascading triggers

### 8. Self-Observing Pipeline

The system exposes its own reasoning chain in real-time, enabling debugging and inspection.

**What's working:**
- SSE debug events: `debug_parse`, `debug_context`, `debug_prompt`, `debug_timing`
- Phase indicators: parsing → interpreting → assembling → generating → calling_tool → extracting
- Preview mode: `POST /api/preview` runs the pipeline dry (classify + assemble + build prompt) without calling the model
- Debug panel in the UI shows full pipeline state
- System messages from MCP servers and build scripts appear inline in chat

### 9. Idea Extraction and Threading

The system automatically extracts structured knowledge from model responses and organizes it into threads.

**What's working:**
- `ExtractionService` — Claude-based extraction of ideas, questions, decisions, and action items from responses
- Extracted items stored as typed nodes with edges back to source turns (`EXTRACTED`)
- Threading: ideas grouped by topic with status tracking (mentioned → explored → parked → resumed)
- `ThreadsSidebar` — UI for browsing ideas by status, with park/resume actions
- Cross-conversation: extracted items are searchable across all conversations via semantic search

## Architecture Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Storage | PostgreSQL 16 | Relational node storage, attributes, LISTEN/NOTIFY |
| Graph | Apache AGE | Cypher queries, cross-domain edges, temporal properties |
| Vector | pgvector | Semantic similarity search (768-dim, cosine distance) |
| Embeddings | Ollama nomic-embed-text | Auto-embedding on store, query-time embedding for search |
| Backend | .NET 8 Minimal API | Context router, chat service, SSE streaming |
| Frontend | React + Vite | Three-panel layout: conversations, chat, debug/planner/threads |
| Agent Interface | MCP (Python) | 3 MCP servers: agent-context, dev, skills |
| Models | Claude (Haiku/Sonnet) | Entity resolution, response generation, extraction |

All in one Postgres instance on port 5433. Single `docker compose up` to start.

## What Sets This Apart

**The DB is the agent's brain, not just its storage.** Most agent frameworks treat memory as a key-value cache or a vector store bolted on the side. Here, the graph structure *is* the reasoning scaffold:
- Parent-child gives hierarchy
- Edges give typed relationships across domains
- Temporal properties give version history
- Embeddings give semantic relevance
- Node types give domain semantics

**Context assembly replaces prompt stuffing.** Instead of "dump the last N messages into the prompt," the system resolves entities and assembles *just the state that matters*. Architecturally closer to a database query planner than a chatbot message buffer.

**Agents coordinate through data, not supervisors.** Plan step statuses create implicit work queues. Advisory locks prevent conflicts. DB triggers spawn agents reactively. No supervisor agent burning tokens watching.

## What's Still POC-Grade

| Limitation | Impact | Fix Path |
|-----------|--------|----------|
| Single Postgres instance | No HA, single point of failure | Managed Postgres or replication |
| Fire-and-forget embeddings | Message N+1 may not see message N's vector | Queue-based embedding with acknowledgment |
| No node versioning | Nodes themselves aren't versioned (only edges are temporal) | `node_versions` table |
| No graph/relational atomicity | AGE sync is best-effort | Outbox pattern |
| C#-only decomposition | Can't ingest Python, TypeScript, etc. | Tree-sitter for multi-language |
| N+1 attribute queries | ContextAssembler.Assemble() has subquery per node | Already fixed in AssembleForAgent (JOIN) |
| No embedding refresh | Edited nodes keep stale embeddings | LISTEN/NOTIFY → re-embed queue |
| Local-only | No team sharing, no cloud deployment | Hosted Postgres + auth layer |

## Token Economics

The core claim: agents with the context store use fewer tokens than agents starting cold.

| Scenario | Cold Start | With Context Store | Mechanism |
|----------|-----------|-------------------|-----------|
| Session orientation | Read CLAUDE.md + grep + read files | Query relevant nodes | Structured retrieval vs linear scan |
| Cross-session continuity | Re-read everything | Query what changed | Temporal edges + semantic search |
| Multi-agent coordination | Each agent pays full orientation cost | One ingest, shared queries | Shared memory via MCP |
| Plan execution | Each step re-orients | Step gets curated context | Entity-driven assembly |

Not yet formally measured — the token measurement harness is in the "what needs building" list (007). But the architectural mechanism is in place: send state, not history.
