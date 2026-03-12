# CodeStoragePoc

Proof of concept: **agent context store** — persistent, queryable, shared memory for AI agents. Code, plans, conversations, and ideas are all nodes in the same Postgres table, connected via an AGE graph. The DB is the agent's brain: it stores what agents learn, decides, and plans. The context router resolves what agents are talking about and assembles curated state around it. See `research/007-agent-context-store.md` for the full vision.

## Quick Start

```bash
# Start Postgres with AGE + pgvector
docker compose up -d --build

# Run the POC demo
dotnet run

# Run the ideation assistant test app
dotnet run --project Api/Api.csproj   # API on http://localhost:5102
cd ui && npm run dev                   # Frontend on http://localhost:5179
```

**Required env vars for the test app:**
- `ANTHROPIC_API_KEY` — Claude API key (required)
- `CODESTORAGE_CONNSTR` — PostgreSQL connection string (optional, defaults to POC credentials)

## Project Structure

```
CodeStoragePoc/
├── Program.cs                     # Main orchestrator — runs the full demo loop
├── CodeStoragePoc.csproj           # .NET 8 console app (Npgsql, Roslyn)
├── DbLayer/
│   ├── Schema.cs                  # DB migration (tables, indexes, triggers)
│   └── NodeRepository.cs          # Node CRUD, subtree fetch, pgvector search
├── GraphLayer/
│   └── AgeLayer.cs                # AGE Cypher queries, vertex/edge sync
├── Decomposer/
│   ├── CSharpDecomposer.cs        # C# source → node tree (Roslyn parser)
│   └── EdgeSeeder.cs              # Scan bodies → CALLS/REFERENCES edges
├── Generator/
│   ├── CSharpGenerator.cs         # Node tree → C# source (the core)
│   ├── PlanToCodeGenerator.cs     # Plan tasks → code nodes + temporal edges
│   └── RoslynValidator.cs         # Parse-level validation before write
├── BuildRunner/
│   └── DotnetBuilder.cs           # dotnet build wrapper
├── AgentCoordination/
│   ├── SubtreeLock.cs             # Advisory locks for concurrent agents
│   ├── ChangeNotifier.cs          # LISTEN/NOTIFY subscriber
│   └── AgentDispatcher.cs         # Event-driven agent spawning from DB events
├── ContextRouter/
│   ├── Types.cs                   # Entity resolution, context payloads, agent contracts, assembled prompts
│   ├── IntentClassifier.cs        # Pattern-based intent detection (regex fallback)
│   ├── InterpreterService.cs      # Entity resolution via Claude Haiku (resolve what user is talking about)
│   ├── ContextAssembler.cs        # Subject-driven state assembly (pgvector, graph, batched queries)
│   ├── PromptBuilder.cs           # Agent contracts + subject state → model-ready XML prompt
│   ├── EmbeddingService.cs        # Ollama nomic-embed-text helper (768-dim, 2s timeout)
│   └── NodeTypes.cs               # String constants for all node types
├── Renderer/
│   ├── PlanRenderer.cs            # Plan tree → readable text
│   └── ConversationRenderer.cs    # Conversation tree → threaded transcript
├── SeedData/
│   ├── Vector2Seeder.cs           # Vector2 struct as 19 nodes
│   ├── PlanSeeder.cs              # Plan 001 as 27 nodes
│   ├── Plan002Seeder.cs           # Plan 002 as 21 nodes
│   ├── Plan003Seeder.cs           # Plan 003 as 25 nodes
│   ├── Plan005Seeder.cs           # Plan 005 with enriched contract attrs
│   ├── CalculatorPlanSeeder.cs    # Calculator plan with code-gen task attrs
│   ├── ConversationSeeder.cs      # Sample conversation as 13 nodes
│   └── ResearchSeeder.cs          # 6 research docs + roadmap root as ~50 nodes
├── Api/
│   ├── Api.csproj                 # Minimal API project (references CodeStoragePoc)
│   ├── Program.cs                 # Endpoints, DI, CORS (port 5102)
│   └── Services/
│       ├── ChatService.cs         # Orchestrates: classify → assemble → tool-use loop → store → extract
│       ├── ExtractionService.cs   # Claude-based idea extraction from responses
│       ├── PlanService.cs         # Plan list + tree queries → DTOs for Planner UI
│       ├── SkillLoader.cs         # Reads skills.json → Anthropic SDK Tool objects (hot reload)
│       ├── PermissionService.cs   # Permission level check, conversation/model-scoped config
│       ├── PendingActionService.cs # Pending action CRUD for approval workflow
│       ├── SystemMessageBus.cs    # Channel-based pub/sub for system messages
│       ├── CodeService.cs         # Decompose/materialize orchestration
│       └── NodeService.cs         # Workspace node queries + CRUD mutations
├── ui/
│   ├── vite.config.ts             # Vite dev server (port 5179), proxy to API
│   └── src/
│       ├── App.tsx                # Three-panel layout, per-conversation state cache, permission selector
│       ├── api.ts                 # Fetch wrappers, SSE streaming, debug/preview/permission types
│       └── components/
│           ├── ChatPanel.tsx      # Concurrent SSE streaming, @model autocomplete, approval cards
│           ├── ConversationList.tsx # Conversation picker with streaming indicators
│           ├── DebugPanel.tsx     # Pipeline debug + preview (parse/intent/context/timing)
│           ├── PlannerPanel.tsx    # Plan list cards + recursive tree viewer
│           ├── ThreadsSidebar.tsx  # Ideas by status, park/resume
│           └── StatsBar.tsx       # Intent, counts, permissions, context debug
├── tools/
│   ├── skills/
│   │   └── skills.json            # Skill definitions (hot-reloaded by SkillLoader + skills-mcp)
│   ├── skills-mcp/
│   │   ├── server.py              # MCP server: dynamic skill discovery + execution
│   │   └── requirements.txt       # mcp, psycopg2-binary
│   ├── dev-mcp/
│   │   ├── server.py              # MCP server: dev environment management
│   │   └── requirements.txt       # mcp, httpx, psycopg2-binary
│   ├── agent-context-mcp/
│   │   ├── server.py              # MCP server: persistent agent memory (store/query/embed/history)
│   │   └── requirements.txt       # mcp, psycopg2-binary, httpx
│   ├── backfill_embeddings.py     # One-time: embed all existing nodes via Ollama (idempotent)
│   ├── test_ui_message.js         # Playwright: verify message persistence after stream
│   ├── test_ui_full.js            # Playwright: verify extraction + message persistence
│   ├── test_ui_tools.js           # Playwright: verify inter-model routing + tool-use phases
│   └── test_ui_planner.js        # Playwright: verify planner tab, list, tree, navigation
├── docker-compose.yml             # Postgres + AGE + pgvector
├── Dockerfile.postgres            # Custom image: AGE base + pgvector
├── init.sql                       # Extension + graph initialization
├── DESIGN.md                      # Architecture decisions
├── research/                      # Research findings (seeded as DB nodes via ResearchSeeder)
│   ├── 001-voice-ai-stack.md      # STT, TTS, voice framework comparisons
│   ├── 002-architecture-review.md # First review findings (superseded by 003)
│   ├── 003-consolidated-review.md # Consolidated Claude + Gemini review (current)
│   ├── 004-temporal-graph-version-control.md  # Temporal edges for version control
│   ├── 005-test-plans-as-nodes.md # Test suites/cases as node types
│   ├── 006-agent-executable-context.md  # Agent runtime, replayable conversations, self-hosted roadmap
│   └── 007-agent-context-store.md # ★ Vision doc — agent context store, MCP server, execution, self-modification
├── plans/
│   ├── 001-voice-ideation-assistant.md
│   ├── 002-ideation-nodes-and-context-router.md
│   ├── 003-review-fixes.md
│   ├── 004-ideation-assistant-test-app.md
│   ├── 005-planner-view.md
│   └── 008-agent-context-mvp.md   # ★ Current — prove token savings with MCP server
└── output/                        # Generated code (created at runtime)
```

## Database Stack

- **PostgreSQL 16** — relational storage for nodes
- **Apache AGE** — graph queries via Cypher (symbol references, dependencies)
- **pgvector** — semantic similarity search on node embeddings

All three in one Postgres instance. Port 5433 (to avoid conflicts).

## Node Types

### Code Domain
`compilation_unit`, `using_directive`, `namespace`, `struct`, `class`, `field`, `constructor`, `method`, `parameter`, `block`, `statement`

### Planning Domain
`plan`, `plan_phase`, `plan_step`, `task`, `risk`, `blocker`, `milestone`, `test_spec`, `revision`, `retrospective`

### Ideation Domain
`conversation`, `turn`, `topic`, `idea`, `question`, `decision`, `action_item`, `thread`, `interpretation`

### Tool-Use & Permission Domain
`tool_call`, `tool_result`, `skill`, `pending_action`

### Research & Roadmap Domain
`roadmap`, `research`, `finding`, `open_question`, `decision`, `reference`

## AGE Edge Labels

### Code Domain
`CALLS`, `REFERENCES`, `DEPENDS_ON`, `MODIFIES`

### Ideation & Planning Domain
`EXTRACTED`, `SPAWNED_FROM`, `IMPLEMENTED_BY`, `PRODUCES`, `CONSTRAINS`, `BLOCKS`, `RELATES_TO`, `CONTRADICTS`

### Research & Roadmap Domain
`INFORMS`

### Agent Action Domain
`PRODUCED`, `TRIGGERED`, `FORKED_FROM`, `OBSERVED`, `INFORMED`

## Conventions

- Raw SQL everywhere — no ORM
- Nodes use gap numbering (sibling_order increments of 100)
- Advisory locks use XOR of UUID halves as 64-bit key
- AGE queries require `LOAD 'age'` + `SET search_path` per connection
- Node types are TEXT, not ENUM — new types are just new string values
- Plans and conversations use file_id = NULL (they don't map to source files)
- Voice and text input produce identical node structures — only `input_mode` attribute differs
- Context router assembles per-model payloads from DB queries, not raw chat history

## Context Router

The context router resolves what the user is talking about and assembles state around it. Models get the state of things, not a replay of how we got there.

### Pipeline (Plan 010 — entity-driven)

1. **InterpreterService** (`ResolveAsync`): Entity resolution via Claude Haiku. Identifies which existing nodes the user is referring to. If confident → resolved entities. If ambiguous → clarification question. Does NOT classify intent.
2. **ChatService**: If unresolved → return clarification question. If direct action (park/resume) → execute without model. Otherwise →
3. **ContextAssembler** (`AssembleFromSubject`): Resolved entity types determine what to pull. Plan nodes → plan state + blockers. Idea nodes → idea subtree. No entities → semantic search + open items. Batched queries (3 total for N nodes, not N×3).
4. **PromptBuilder** (`BuildFromSubjectState`): Agent contract (function/input/output/constraints) + subject state → XML prompt. No chat history, no prose role prompts.

**Core principles:**
- No chat history in model context — send state, not conversation replay
- Entity types drive context assembly — not a predetermined intent category
- Ask don't assume — when resolution fails, return a clarification question
- Agents defined by input/output/function contracts, not prose descriptions

### Interpreter Resolution

The Interpreter's ONLY job is figuring out WHAT the user is talking about:

- **Entity resolution**: matches user references ("the voice thing", "that plan") to existing node IDs via conversation context + semantic search
- **Project resolution**: new, existing (with resolved project ID), or cross_project
- **Direct action detection**: park/resume detected by keyword, but entities still resolved by the model
- **Clarification path**: if ambiguous, returns `ClarificationQuestion` + `CandidateMatches` — system structurally cannot proceed with unresolved input
- **Stored as nodes**: Each resolution stored as `interpretation` node with structured attributes

Config: `ANTHROPIC_API_KEY` env var (required), `INTERPRETER_MODEL` env var (default: `claude-haiku-4-5-20251001`). 15s timeout. Falls back to regex `IntentClassifier` on failure.

### Agent Contracts

Agents are defined by structured contracts, not prose:

| Field | Purpose |
|-------|---------|
| `Function` | One-sentence description + specific entity names being operated on |
| `InputDescription` | What the agent receives (resolved subject + state) |
| `OutputDescription` | What the agent should produce |
| `Constraints` | Things the agent should NOT do |

Contracts are generated dynamically by `BuildContract()` based on resolved subject types (plan domain, code domain, idea domain, research domain).

### Subject-Driven State Assembly

`AssembleFromSubject()` pulls state based on what was resolved:

- **ResolvedNodeStates**: Full state of each resolved node (attributes, child count, child types) — batched in 3 queries total
- **ConnectedNodes**: Graph neighbors via AGE, filtered by domain-specific edge types (`GetRelevantEdgeTypes`)
- **RelatedNodes**: Semantically similar nodes via pgvector (cross-project, cross-conversation)
- **OpenItems**: Blockers, risks, open questions relevant to the subject domain

`DisplayIntent` on `ResolvedSubject` maps entity types to intents for SSE compat only — it does NOT drive context assembly.

### Legacy Pipeline (backward compat)

The intent-driven pipeline still works via wrapper methods:
- `InterpretAsync()` wraps `ResolveAsync()` → `InterpretedInput`
- `Assemble()` / `AssembleForAgent()` → `ContextPayload` / `AgentContext`
- `Build()` → intent-driven `AssembledPrompt`

Used by: `PreviewAsync` endpoint, `Program.cs` demo.

### Agent Context Model

Two modes for context assembly:
- **SubjectState** (`AssembleFromSubject()`): Entity-driven — full node state, graph neighbors, semantic relatives, open items
- **ContextPayload** (`Assemble()`): Intent-driven (legacy) — pre-fetched data for simple consumers
- **AgentContext** (`AssembleForAgent()`): Lightweight NodeRef objects (ID + summary) for agents that navigate the tree themselves

Turn nodes track audience via `target` (user/agent/self) and optional `target_agent` attributes.

### Semantic Search Pipeline (Plan 009)

The context assembler uses pgvector cosine similarity instead of ILIKE substring matching:

- **EmbeddingService** (`ContextRouter/EmbeddingService.cs`): Ollama nomic-embed-text helper. 768-dim vectors. `search_document:` prefix for storage, `search_query:` prefix for queries. 2s timeout, returns null on failure.
- **Auto-embedding**: Every turn (user + model) and extracted item (idea, question, decision) is embedded on storage via fire-and-forget `Task.Run`. Node stores immediately; embedding follows asynchronously.
- **Semantic search**: `SemanticSearchNodes()` in ContextAssembler queries ALL nodes (cross-project, cross-conversation) via `embedding <=> vec::vector` cosine distance. No conversation scope — the whole DB is the context.
- **ILIKE fallback**: If Ollama is down (EmbedQueryAsync returns null), falls back to existing ILIKE substring search. Degraded but functional.
- **SearchCoverage**: `ContextPayload.Coverage` reports embedded/total/pct. PromptBuilder emits a `<warning>` when coverage < 50%.
- **Similarity scores**: Included in `<relevant_ideas>` and `<cross_session_context>` XML as `similarity="0.XXX"` attributes. Also in `debug_context` SSE event.

**Known limitation**: Fire-and-forget means message N+1 may not see message N's embedding yet. Acceptable for POC — message N+2 will, and backfill_embeddings.py catches up offline.

## Skills & Tool-Use

Models have access to tools (skills) that let them interact with the system and each other.

### Architecture

1. **`tools/skills/skills.json`** — central config defining all skills. Hot-reloaded on every request.
2. **`SkillLoader`** — reads skills.json, converts to Anthropic SDK `Tool` objects for `MessageCreateParams.Tools`.
3. **ChatService tool-use loop** — up to 5 iterations: send message with tools → model responds with `ToolUseBlock` → execute tool → send `ToolResultBlockParam` back → repeat until no more tool calls.
4. **`tools/skills-mcp/server.py`** — Python MCP server exposing `list_skills` and `execute_skill` for Claude Code to use directly.

### Built-in Skills

| Skill | Handler | Purpose |
|-------|---------|---------|
| `route_to_model` | builtin (ChatService) | Inter-model communication — `@haiku tell sonnet to...` |
| `search_ideas` | inline DB query | Search ideas/questions/decisions by text |
| `get_node_details` | inline DB query | Get node with attributes and children |
| `list_threads` | inline DB query | List conversation threads by status |

`route_to_model` makes a non-streaming call to the target model with no tools (prevents recursion).

### SSE Events for Tool-Use

| Event | Payload | When |
|-------|---------|------|
| `tool_call` | `{ name, toolId }` | Model requests a tool call |
| `tool_result` | `{ name, toolId, resultLength }` | Tool execution completes |
| `phase` | `{ phase: "calling_tool" }` | Before tool execution |

The frontend shows tool name in the phase indicator: "Calling route_to_model..."

## Agent Permissions

Controls what models can do within conversations. Four levels, stored as conversation attributes.

### Permission Levels

| Level | Label | Read | Create | Mutate | Delete |
|-------|-------|------|--------|--------|--------|
| 0 | Observe | yes | no | no | no |
| 1 | Suggest | yes | propose | no | no |
| 2 | Assist | yes | yes | propose | no |
| 3 | Auto | yes | yes | yes | propose |

### Configuration

Each skill has a `permissionLevel` in `skills.json`. Each conversation has a default permission level stored as the `permission_level` attribute (default: 2 = Assist). Per-model overrides use `permission:{model}` attributes (e.g., `permission:haiku` = `1`).

Resolution: per-model override > conversation default > system default (2).

### Services

- **PermissionService**: `CheckPermission(conversationId, model, skill)` → Allowed / NeedsApproval / Denied. Reads conversation attributes. `GetPermissionConfig` / `SetConversationPermission` / `SetModelPermission` for config CRUD.
- **PendingActionService**: Creates `pending_action` nodes when a model needs approval. `ApproveAction` / `DenyAction` resolve them.

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/permissions/{conversationId}` | GET | Current permission config |
| `/api/permissions/{conversationId}` | POST | Set default or per-model permission |
| `/api/actions/{conversationId}` | GET | List pending actions |
| `/api/action/{id}/approve` | POST | Approve a pending action |
| `/api/action/{id}/deny` | POST | Deny a pending action |

### SSE Events

| Event | Payload | When |
|-------|---------|------|
| `permission_request` | `{ actionId, model, skill, description, paramsJson }` | Model tries action above its level |

### Frontend

- **Header**: Permission level selector (Observe/Suggest/Assist/Auto) — clickable to change, color-coded
- **ChatPanel**: Inline approval cards for pending actions (Approve/Deny buttons)
- **StatsBar**: Shows current permission level and per-model overrides

## Plan-to-Code Generation

Plans can drive code generation. Task nodes carry structured code-gen attributes; `PlanToCodeGenerator` reads them to produce code nodes without free-text parsing.

### How It Works

1. **Plan root** carries `target_namespace` and `target_class` attributes
2. **Task nodes** carry `method_name`, `return_type`, `params`, `body` attributes
3. `PlanToCodeGenerator.GenerateFromPlan()` walks the plan tree, finds tasks with code-gen attrs, creates code nodes (compilation_unit → namespace → class → methods)
4. Creates `IMPLEMENTED_BY` temporal edges from each task to its generated method
5. `CSharpGenerator` materializes the code nodes into compilable C#

### Mutation + Temporal Edges

When a plan is revised (task modified or added):
1. `RegenerateFromPlan()` closes old `IMPLEMENTED_BY` edges (sets `valid_to`)
2. Deletes old code nodes for the file
3. Creates new code nodes and new `IMPLEMENTED_BY` edges (with `valid_from`)
4. Temporal edge queries show the full version history

### Temporal Edge Properties

| Property | Type | Purpose |
|----------|------|---------|
| `valid_from` | ISO timestamp | When this edge was created |
| `valid_to` | ISO timestamp or absent | When this edge was superseded (NULL = current) |
| `provenance` | string | What created the edge (e.g., "plan-to-code", "decomposer") |

### Cross-Project Edges

Plan tasks live in `planProjectId`, code nodes in `projectId`. `IMPLEMENTED_BY` edges cross project boundaries in AGE. Both projects' vertices must be synced before creating edges.

### Key Constraint

Task attributes must be structured (`method_name`, `return_type`, `params`, `body`). Free-text task descriptions are NOT parsed for code generation. The `params` attribute uses simple comma-split (`"int a, int b"`) — works for simple types but not generics like `Dictionary<string, int>`.

## Agent Dispatcher

Event-driven outbound agent spawning. The system proactively creates agents when interesting DB events happen — it doesn't wait for user messages.

**Flow:** `pg_notify(node_changed)` → `AgentDispatcher` → match trigger rules → spawn CLI agent → push result to chat UI via `SystemMessageBus`

### Trigger Rules (`tools/skills/triggers.json`)

Hot-reloaded JSON config. Each rule defines:
- `nodeTypes`: which node types trigger this rule
- `conditions`: optional attribute value regex matches
- `agent`: which CLI model to spawn (claude, gemini, codex)
- `promptTemplate`: what to tell the agent (node context is prepended automatically)

### Built-in Triggers

| Trigger | Node Types | Condition | Agent | Purpose |
|---------|-----------|-----------|-------|---------|
| Idea Reviewer | idea | status = mentioned/explored | gemini | Poke holes in new ideas |
| Plan Step Verifier | plan_step, task | status = completed | claude | Verify completion, suggest follow-ups |
| Decision Logger | decision | status = committed | gemini | Summarize + flag cross-cutting concerns |
| Stale Thread Nudge | idea, question | status = mentioned | claude | Nudge user to explore or park (disabled) |

### Debouncing

30-second window per node ID prevents cascading triggers (e.g., node created then immediately updated).

### Adding New Triggers

Drop a new entry in `triggers.json`. No code changes needed — rules are hot-reloaded on every notification.

## Connection & Locking Patterns

- **NodeRepository factory pattern**: Constructor accepts optional `NpgsqlConnection` + `NpgsqlTransaction?`. If provided, uses that connection for all queries (lock-safe). If not, opens a new connection per operation (read-only convenience).
- **SubtreeLock** implements `IAsyncDisposable`. Use `await using`. Exposes `LockConnection` so mutations happen on the same session that holds the advisory lock.
- **InsertNode** is transactional: node + attributes are wrapped in BEGIN/COMMIT/ROLLBACK.
- **AGE MERGE** matches on `node_id` only, then SET name/type — prevents duplicate vertices when properties change.
- **DotnetBuilder** reads stdout/stderr concurrently via `Task.WhenAll` to prevent deadlock.
- **Vector dimensions**: 768 (nomic-embed-text via Ollama). Schema, seeder, and search all use 768.

## Known Issues (from review)

See `research/003-consolidated-review.md` for full details. Plan 003 tracks fixes.

### Fixed (Plan 003 — completed)
- ~~Advisory lock session mismatch~~ — NodeRepository accepts external connection, mutations use lock session
- ~~Cypher injection~~ — edge type whitelist + EscapeCypher() for values
- ~~Lock release ordering~~ — release after validation, not before
- ~~Hardcoded credentials~~ — CODESTORAGE_CONNSTR env var with POC fallback
- ~~No transaction boundaries~~ — InsertNode wraps node+attributes in transaction
- ~~Sequential context assembly~~ — Task.WhenAll for parallel queries
- Added NodeTypes.cs with string constants for all node types

### Design Debt (deferred)
- **N+1 attribute subqueries** in ContextAssembler.Assemble() (AssembleForAgent uses JOIN)
- **Intent classifier brittleness** (fix: model-based classification for production)
- **No graph/relational atomicity** (fix: outbox pattern for AGE sync)
- **No node versioning** (fix: node_versions table before production code gen)
- **No embedding refresh** (fix: LISTEN/NOTIFY subscriber → re-embed queue)

## MCP Servers

### Dev Environment — `codestoragepoc-dev`

`tools/dev-mcp/server.py` — registered as `codestoragepoc-dev` in Claude Code.

| Tool | Purpose |
|------|---------|
| `check_status` | Report Docker/API/Vite/DB status in one call |
| `start_api` | Kill stale Api.exe, build, start, verify health |
| `start_frontend` | Start Vite if not running (npm install if needed) |
| `verify_config` | Check env vars, DB connectivity, FK integrity, extensions |
| `stop_all` | Clean shutdown of API and frontend (not Docker) |
| `build` | Kill stale → build → start → health check → push system message to UI |
| `test_chat` | Quick (health + preview) or full (round-trip SSE) pipeline test |

**Usage:** Call `build` after backend changes. Call `test_chat` to verify the pipeline works. Both push results as system messages visible in the chat UI.

### Skills — `codestoragepoc-skills`

`tools/skills-mcp/server.py` — register as `codestoragepoc-skills` in Claude Code.

| Tool | Purpose |
|------|---------|
| `list_skills` | Return full skill catalog from skills.json |
| `execute_skill` | Execute a skill by name with JSON params (DB queries) |

**Registration:** `claude mcp add codestoragepoc-skills -s project -- python tools/skills-mcp/server.py` (run from project root)

### Agent Context — `agent-context`

`tools/agent-context-mcp/server.py` — persistent memory for AI agents. Store and retrieve decisions, findings, ideas, and plans as nodes in the graph DB. This is how Claude Code conversations accumulate knowledge across sessions.

| Tool | Purpose |
|------|---------|
| `ensure_project` | Get or create a project by name (returns stable ID) |
| `store_node` | Insert a node with attributes. **Auto-embeds** via Ollama (nomic-embed-text, 768-dim). Falls back silently if Ollama unavailable. |
| `query_nodes` | Text search across nodes by name/value (ILIKE substring match) |
| `semantic_search` | **Meaning-based** search via pgvector cosine similarity. Returns ranked results + coverage metric (embedded/total/pct). Requires Ollama. |
| `get_node` | Fetch a node with attributes and children |
| `get_children` | List children of a node, optionally filtered by type |
| `list_projects` | List all projects with node counts + embedding counts |
| `list_recent` | Recent activity across the context store |
| `history` | Temporal edge history for a node via AGE graph (valid_from, valid_to, provenance, direction) |

**Auto-embedding:** `store_node` computes a 768-dim embedding via Ollama (`nomic-embed-text`) with `search_document:` prefix. 2s timeout — if Ollama is down or busy, the node stores without an embedding. `semantic_search` uses `search_query:` prefix. Backfill existing nodes: `python tools/backfill_embeddings.py` (idempotent, `--dry-run` to preview, `--force` to re-embed all).

**Usage:** Call `ensure_project` to get a project ID, then `store_node` to persist decisions/findings as you work. Use `semantic_search` for conceptual queries ("how does auth work?") or `query_nodes` for exact text matches. Use `history` to see how a node's relationships evolved. Use `get_node`/`get_children` to navigate the tree.

**Registration:** Registered globally in `~/.claude.json` and via `.mcp.json` at project root.

## SSE Debug Events

The API emits debug events alongside regular streaming events:

| Event | Payload | When |
|-------|---------|------|
| `debug_parse` | originalMessage, mention, cleanedMessage, model, durationMs | After @mention parsing |
| `debug_context` | nodeCount, nodes[], durationMs | After context assembly |
| `debug_prompt` | systemPromptLength, userPromptLength, tokenEstimate | After prompt building |
| `debug_timing` | parseMs, classifyMs, contextMs, generateMs, extractMs, totalMs | After full pipeline |
| `phase` | phase (e.g., "extracting") | Before extraction starts |
| `system_message` | level, text, conversationId | Via `GET /api/events` SSE broadcast |

The frontend shows debug events in the Debug tab (right panel) and in Preview mode (dry-run via `POST /api/preview`).

## System Messages

External tools (MCP server, build scripts) can push messages into the chat UI in real-time:

- **`POST /api/system-message`** — `{ text, level?, persist?, conversationId? }` — broadcasts to all connected clients
- **`GET /api/events`** — SSE endpoint the frontend connects to via native `EventSource`
- **`SystemMessageBus`** — in-memory Channel-based pub/sub (bounded, drop-oldest)

Levels: `info` (green badge), `warn` (yellow), `error` (red). System messages render as italic/muted text in chat.

## Plan Contract

Plans use an OKR + WBS hybrid model. The plan node IS the objective (name = goal, value = description). Parent-child relationships provide context — a question under a risk is a risk question, a blocker under a step blocks that step.

### Plan Attributes
- `plan_type`: feature, bugfix, roadmap, spike
- `priority`: p0, p1, p2
- `target_date`: ISO date
- `status`: pending, in_progress, completed, blocked, proposed, committed

### Node Types in a Plan
| Type | Purpose | Key Attributes |
|------|---------|---------------|
| `plan` | Root — the objective | plan_type, priority, target_date |
| `plan_phase` | Lifecycle phase (GATHER, PLAN, APPROVE, EXECUTE, CLOSE_OUT) | status |
| `plan_step` | Work package under a phase | status |
| `task` | Atomic work item | status |
| `risk` | Threat to plan success | severity (low/medium/high), abort_trigger, mitigation |
| `blocker` | Active impediment | status (blocking/resolved), owner |
| `milestone` | Checkpoint / deliverable | status |
| `decision` | Committed choice | status (proposed/committed) |
| `question` | Open question | status, proposed_answer |
| `test_spec` | Verification criteria | test_type, status |
| `retrospective` | Post-mortem | template |

### API Endpoints
- `GET /api/plans` — list all plans with type/status/priority badges
- `GET /api/plan/{id}` — full recursive tree with all descendants

### Planner UI
Right-panel tab showing plan list (cards) and drill-down tree view. Node type icons, status badges, expand/collapse with 12px indent. First 2 levels expanded by default.

## Code Decomposition & Materialization

Full round-trip: C# source → structured nodes → C# source. Ported from db-ast-poc.

### Pipeline
1. **Decompose**: `CSharpDecomposer` parses C# with Roslyn → deep node tree (compilation_unit > namespace > class > method > parameter + block > statement)
2. **Edge Seed**: `EdgeSeeder` scans statement text for field/method references → CALLS/REFERENCES edges in AGE
3. **Materialize**: `CSharpGenerator.GenerateFromFile()` walks node tree → emits valid C# source

### Decompose Strategy
- Delete-and-reinsert per file (idempotent, IDs change on re-decompose)
- Nodes stored with `node_attributes` (access, type, return_type, modifier, signature)
- sibling_order gap numbering preserves syntax order
- Supports file path (disk) or inline source text

### API Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/code/decompose` | POST | `{ projectId, filePath, sourceText? }` → decompose into nodes |
| `/api/code/materialize` | POST | `{ fileId? \| rootNodeId? }` → generate C# source |
| `/api/code/files/{projectId}` | GET | List tracked files with node counts |

### Node Structure (example: Vector2.cs → 20 nodes)
```
compilation_unit
├── using_directive "System"
└── namespace "CodeStoragePoc.Generated"
    └── struct "Vector2" [attrs: access, signature]
        ├── field "X" [attrs: access, type]
        ├── field "Y" [attrs: access, type]
        ├── constructor [attrs: access, parent_type_name]
        │   ├── parameter "x" [attrs: type]
        │   ├── parameter "y" [attrs: type]
        │   └── block
        │       ├── statement "X = x;"
        │       └── statement "Y = y;"
        ├── method "Add" [attrs: access, return_type]
        │   ├── parameter "other" [attrs: type]
        │   ├── parameter "scalar" [attrs: type]
        │   └── block
        │       └── statement "return new Vector2(...);"
        └── method "ToString" [attrs: access, modifier=override, return_type]
            └── block
                └── statement "return $\"({X}, {Y})\";"
```

## Concurrent Streaming

ChatPanel supports multiple concurrent SSE streams. Each stream tracks:
- Model + provider (from @mention routing)
- Pipeline phase: parsing → interpreting → assembling → generating → calling_tool (if tools used) → extracting
- Intent (from interpreter)
- Response buffer

The input is never disabled — users can type and send to multiple models simultaneously.

### Per-Conversation State Cache

App.tsx holds a `Map<conversationId, ConversationState>` ref. Each conversation keeps its own messages, threads, stats, debug log, permissions, and streaming status. Switching conversations changes which cached state is displayed — active streams continue writing to their original conversation via `streamConvIdRef`. The conversation list shows a pulsing blue dot next to conversations with active streams.

Key design: `streamConvIdRef` tracks the "real" conversation ID for callbacks. When a new conversation starts (null → UUID migration), the ref updates so all subsequent callbacks write to the correct cache entry.

## UI Testing (Playwright)

Headless browser tests in `tools/` verify the full pipeline end-to-end:

```bash
# Simple message persistence (no tools)
node tools/test_ui_message.js

# Extraction + persistence
node tools/test_ui_full.js

# Inter-model routing + tool-use phases
node tools/test_ui_tools.js

# Planner tab: list, tree, navigation
node tools/test_ui_planner.js
```

**Requires:** API running on 5102, frontend on 5179, `npm install playwright` in project root.

## Git Workflow

- workflow: direct
- base_branch: main
