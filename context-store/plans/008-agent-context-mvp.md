# Plan 008 — Agent Context Store MVP

**Level: L2**
**Status: in_progress (Phase 3 — Dog-fooding)**
**Goal: Prove that agents with a context store use fewer tokens than agents starting cold.**

## Progress

### Phase 1: MCP Server (complete)
Built 7 CRUD tools: `ensure_project`, `store_node`, `query_nodes`, `get_node`, `get_children`, `list_projects`, `list_recent`. All working, registered in `.mcp.json`, callable from Claude Code.

Also built (not in original plan): `tools/dev-mcp/`, `tools/skills-mcp/`, `tools/query_db.py`.

### Phase 2: Semantic Search + History (complete)
Filled gaps between original plan and implementation:
- **Auto-embedding** on `store_node` via Ollama (nomic-embed-text, 768-dim, 2s timeout, silent fallback)
- **`semantic_search`** tool — pgvector cosine similarity with coverage metric
- **`history`** tool — AGE Cypher temporal edge queries (both directions)
- **`backfill_embeddings.py`** — idempotent script with dimension pre-flight, `--dry-run`, `--force`
- **Backfill run**: 206 nodes embedded (2026-03-10)
- **Ground-truth quality test passed**: 3/3 correct semantic rankings (auth/db/react)
- **Bug fix**: `semantic_search` param ordering — vector params now correctly positioned relative to filter params

### Phase 3: Dog-fooding (current)
Using the MCP server on real tasks to accumulate context and identify gaps.

**Goal**: 2+ weeks of real usage building up decisions, findings, and ideas in the DB.

### Phase 4: Measurement (pending)
A/B token comparison once enough context is accumulated from dog-fooding.

## Success Criteria

### Done
- [x] `agent-context` MCP server running and callable from Claude Code (9 tools)
- [x] `store_node` persists agent-created nodes with auto-embedding
- [x] `query_nodes` finds nodes by text search (ILIKE)
- [x] `semantic_search` returns relevant nodes ranked by cosine similarity
- [x] `semantic_search` returns coverage metric (embedded/total/pct)
- [x] Ground-truth quality test passes (auth/db/react ranking — 3/3 correct)
- [x] `history` returns temporal edge history for a node via AGE Cypher
- [x] `get_node` / `get_children` / `list_projects` / `list_recent` all working
- [x] `list_projects` includes embedding counts
- [x] Backfill script with dimension pre-flight, `--dry-run`, `--force`, idempotent
- [x] Backfill run on existing nodes (206 nodes, 2026-03-10)
- [x] CLAUDE.md updated with all tools + auto-embedding docs

### Pending
- [ ] At least 2 weeks of dog-fooding data accumulated
- [ ] Side-by-side token comparison on a real task
- [ ] Results documented in research/008-measurement-results.md

## Key Risks

### Context router built for conversations, not code
The intent classifier was designed for ideation (deepening, parking, recalling). Code tasks
("add a method", "fix a bug", "refactor this class") don't map to existing intents.

**Resolution:** `semantic_search` IS the get_context for agent use cases. Pattern-based
intent classification is conversation-specific. Agents query by meaning, not intent.
Formal context router integration deferred until dog-fooding shows what intents agents need.

### Token measurement difficulty
Claude Code doesn't expose per-session token counts directly.

**Mitigation:** Use `claude --output-format json` which includes token usage, or measure
via API billing dashboard delta between runs. Alternatively, count tool calls (file reads,
greps) as a proxy — fewer reads = fewer tokens.

### Embedding drift
If nomic-embed-text model version changes, old and new embeddings live in different
vector spaces, degrading search quality.

**Mitigation:** `backfill_embeddings.py --force` re-embeds all nodes. Document model tag used.

## Deferred Items

Each deferred item has full design context preserved below. Nothing is lost — when we're
ready to build these, the design decisions, security requirements, testing plans, and
dependencies are all here.

| Item | Why Deferred | When to Build |
|------|-------------|---------------|
| **`execute` tool** | Highest-risk task (arbitrary code execution). Orthogonal to thesis. Needs Docker sandbox, timeout, output cap, namespace restrictions. Own L2 plan. | After proving memory value. Security review required. |
| **`get_context` / context router** | `semantic_search` IS get_context for agents. Building router before usage data would be speculative. | After 2+ weeks of dog-fooding shows what intents agents need. |
| **`ctx ingest` CLI** | Ingest is about code structure. Thesis is about agent memory (decisions/findings). Separate hypothesis. | After proving semantic search on agent-created nodes. |
| **Token measurement** | Needs accumulated context from real usage first. | After 2+ weeks of dog-fooding. |
| **`ctx watch`** | Stretch goal. Depends on ingest existing first. | After ingest CLI. |

## What We're NOT Building Yet

- **Dispatcher** (auto-spawning agents) — next plan if token savings prove out
- **Full self-modification** — nodes as executable skills is the vision, not the MVP
- **Branch model** — requires schema work, defer
- **Self-pruning** — need usage data first, which we get from running the MVP
- **Web UI changes** — terminal only for MVP
- **Multi-agent coordination** — single agent test first
- **Production security** — local Docker, POC credentials
- **Test suite for MCP tools** — validated manually + ground-truth test. Formalize after dog-fooding surfaces edge cases.

---

## Deferred Items — Full Design Context

### Plan 008-execute: C# Snippet Execution Tool

**What:** MCP tool `execute(source, project_name?)` that compiles and runs a C# snippet.

**Why it was deferred:** L2 review (Claude + Gemini) flagged this as the highest-risk task.
Arbitrary code execution from an LLM-driven tool on the host machine is dangerous. The tool
is also orthogonal to the plan's thesis ("agent memory saves tokens") — you can prove memory
value without execution.

**What was designed (carry forward):**
- Write source to temp .cs file in a temp directory with a minimal .csproj
- Run `dotnet build` + `dotnet run`, capture stdout/stderr
- Return success/failure + output
- Clean up temp directory after
- Store the execution as a `tool_result` node (tracks what the agent ran)

**Security requirements identified in review:**
- **Sandbox:** Run inside an ephemeral Docker container (not on the host). Use the existing
  Dockerfile.postgres pattern — create a Dockerfile.execute with .NET SDK, copy source in,
  build+run, destroy container. Or use `dotnet-script` in a restricted environment.
- **Timeout:** Hard 30s limit on execution. Kill process on timeout.
- **Output cap:** Truncate stdout/stderr to 10KB to prevent context window overflow.
- **No network:** Container should have `--network none` to prevent exfiltration.
- **Restricted namespaces:** Block `System.IO.File.Delete`, `System.Diagnostics.Process`,
  `System.Net.Http` at compile time (or via Roslyn analyzer).
- **No persistent state:** Temp directory deleted after execution, container destroyed.

**Testing designed:**
- `execute('Console.WriteLine("hello");')` → stdout contains "hello"
- Syntax error → failure + error message
- Infinite loop → timeout after 30s, process killed
- 10MB output → truncated to 10KB
- `File.Delete("C:\\")` → blocked at compile or sandbox
- Temp directory cleaned up after execution
- Execution stored as `tool_result` node

**Dependencies:** Docker SDK or subprocess calls, .NET SDK image, security review.

**Estimated scope:** L2 plan on its own (security + sandboxing is non-trivial).

---

### Plan 008-context-router: Intent-Driven Context Assembly

**What:** MCP tool `get_context(input, project_id?)` that calls the context router to return
curated, intent-driven results instead of raw node lists.

**Why it was deferred:** The C# ContextAssembler is conversation-focused (ideation, deepening,
parking, resuming). Agent memory queries are different — "what did we decide about X?" maps
to semantic search, not intent classification. `semantic_search` IS the `get_context` for
agent use cases right now.

**What would be needed:**
- New intents: `code_navigation`, `code_modification`, `code_review` (from plan v1 risks)
- Intent-to-query mapping for agent use cases (not conversation use cases)
- Either: port ContextAssembler logic to Python, or call the C# API from MCP server

**When to build:** After dog-fooding semantic_search + history, we'll know what intents
agents actually need. Building the router before we have usage data would be speculative.

**Prerequisite:** Usage data from dog-fooding the current MCP server.

---

### Plan 008-ingest: Source File Ingestion Pipeline

**What:** `ctx ingest` CLI that walks a directory, decomposes source files into nodes, and
computes embeddings.

**Why it was deferred:** The thesis is "agent memory saves tokens." Ingest is about code
structure (classes, methods, parameters as nodes). The token savings come from agents
querying prior decisions/findings, not from having code structure in the DB. Code structure
is valuable but it's a separate hypothesis.

**What was designed (carry forward):**
- Python CLI: `python tools/ctx.py ingest <directory>`
- Options considered:
  a) tree-sitter (language-agnostic, structural parsing) — plan v1's proposal
  b) Regex-based extraction (class/method signatures) — fast but shallow
  c) LLM summarization per file → nodes with embeddings — accurate but expensive
  d) Shell out to CSharpDecomposer (Roslyn) — precise for C# only
- Stores: file → compilation_unit → namespace → class → method → parameter nodes
- Computes embeddings via Ollama on method/class summaries
- Existing C# decomposer in Decomposer/CSharpDecomposer.cs is the gold standard for C#

**When to build:** After proving semantic search on agent-created nodes. If semantic search
proves valuable, extending to code structure is the natural next step.

---

### Plan 008-measurement: Token Comparison Harness

**What:** A/B test comparing cold-start vs context-store token usage on a real task.

**Why it was deferred:** Need the tools working first. Can't measure improvement if the
tools don't exist yet.

**What was designed (carry forward):**
- Pick a repeatable task on CodeStoragePoc (e.g., "add a bookmark node type")
- Run A: Claude Code cold start, no context MCP — measure tokens
- Run B: Same task, with `agent-context` MCP connected — measure tokens
- Compare: total tokens, file reads, time to first meaningful action
- Measurement options:
  a) `claude --output-format json` includes token usage
  b) API billing dashboard delta between runs
  c) Count tool calls (file reads, greps) as proxy — fewer reads = fewer tokens
- Document in research/008-measurement-results.md

**When to build:** After 2+ weeks of dog-fooding the MCP server. Need real accumulated
context in the DB for Run B to have an advantage.

**Prerequisite:** Enough stored nodes (decisions, findings, ideas) from real usage.

---

## Review History

- **v1**: Original plan (5 steps). Built 7 CRUD tools, missed semantic search / history / execute / ingest.
- **v2**: Gap analysis. Added semantic search, history, auto-embedding, backfill. Deferred execute (security). Reviewed by Claude + Gemini.
- **v2 merged into v1** (2026-03-10): Consolidated into single plan file. v2 working document deleted.

### Review Findings Applied (from v2)
| Finding | Source | Resolution |
|---------|--------|------------|
| `execute` needs sandboxing — arbitrary code on host is dangerous | Gemini | Deferred to Plan 008-execute (full context preserved) |
| Move backfill earlier — semantic_search can't be tested without embeddings | Both | Backfill moved to Task 2 (done) |
| Sync embedding bottleneck if Ollama busy | Gemini | 2s timeout, skip silently, log warning |
| Verify vector(768) not vector(1536) | Gemini | Dimension pre-flight in backfill script |
| semantic_search should report coverage | Claude | Coverage field in response (done) |
| Backfill needs idempotency | Gemini | WHERE embedding IS NULL + --dry-run flag (done) |
| AGE property index for history performance | Gemini | Verification step in history tool |
| Embedding quality ground-truth test | Gemini | 3-concept ranking test (passed) |
