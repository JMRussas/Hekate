# 007 — Agent Context Store

## The Reframe

CodeStoragePoc started as "store code as nodes in Postgres." The real value is broader: **persistent, queryable, shared memory for AI agents.** The database isn't storing code — it's storing everything agents learn, decide, and plan. Code decomposition is one input source among many.

The context store is the agent's brain. It's also the orchestrator, the execution engine, the trust boundary, and the skill library. Agents don't need filesystem access or shell commands — they need `get_context`, `execute`, and `store_result`. Everything goes through the context store, so everything is tracked.

## Problem Statement

Every AI agent session starts cold. Claude Code reads CLAUDE.md, greps files, reads source code to orient itself. This "understanding tax" costs thousands of tokens per session and scales with project size. When multiple agents work on the same project, each pays this tax independently. Nothing learned in one session carries over unless manually written to flat text files.

**Flat memory files (CLAUDE.md, MEMORY.md) are limited:**
- Not queryable — the agent reads linearly, can't ask "what relates to auth?"
- Not structured — decisions, ideas, and code understanding all mixed together
- Not shared — each agent has its own memory files, no cross-agent coordination
- Not temporal — no history of what changed when and why
- Scale poorly — 200-line MEMORY.md is already truncated

## What the Agent Context Store Provides

A Postgres database (with pgvector + AGE) that serves as shared agent memory:

### 1. Persistent Understanding
Agent decomposes a file → structural understanding stored as nodes. Next session, any agent queries the nodes instead of re-reading the file. One read, many consumers.

### 2. Cross-Domain Knowledge Graph
Plans, ideas, conversations, decisions, and code all connected via typed edges. "Show me every idea that became a plan that became code" is one query. The cross-domain links ARE the value — no other tool provides this.

### 3. Intent-Driven Context Assembly
The context router replaces "dump everything into the prompt" with "give the agent exactly what it needs for this task." Different intents pull different data. Fewer tokens, better focus.

### 4. Temporal History
Temporal edges track what changed when and why. "Which version of the plan produced which version of the code?" is a graph query. Full audit trail for AI-generated work.

### 5. Coordination Without Supervisors
Advisory locks on subtrees prevent conflicts. Plan step statuses create an implicit work queue. Agents self-assign by querying for unclaimed work. No supervisor agent burning tokens watching.

## Token Savings Model

| Scenario | Without Context Store | With Context Store | Savings |
|----------|----------------------|-------------------|---------|
| Session orientation (50-file project) | ~15,000 tokens (read CLAUDE.md + grep + read files) | ~2,000 tokens (query relevant nodes) | ~87% |
| Cross-session continuity | Re-read everything | Query what changed since last session | ~90% |
| Multi-agent (3 agents, same project) | 3 × 15,000 = 45,000 tokens | 15,000 (first ingest) + 3 × 2,000 = 21,000 | ~53% |
| Plan execution (5-step plan) | 5 × 15,000 = 75,000 tokens (each step re-orients) | 15,000 + 5 × 2,000 = 25,000 | ~67% |

These are orientation tokens only — not counting the actual work tokens. The savings compound with project size and session count.

Even with growing context windows, filling them with irrelevant context is slower, more expensive, and increases the chance of the model losing focus. The context store isn't fighting window limits — it's making better use of whatever window you have.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      Agent Context Store                         │
│                                                                  │
│  ┌───────────┐  ┌───────────┐  ┌─────────────────────────────┐  │
│  │ Postgres  │  │ pgvector  │  │  AGE (or relational edges)  │  │
│  │ (nodes)   │  │ (semantic)│  │  (cross-domain, temporal)   │  │
│  └───────────┘  └───────────┘  └─────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Context Router                               │   │
│  │  classify intent → assemble context → build prompt        │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Execution Engine                             │   │
│  │  materialize nodes → compile → sandbox → run → capture    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              MCP Server (agent-context)                    │   │
│  │  get_context | execute | mutate | store_result |          │   │
│  │  claim_work | complete_work | query | history             │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Dispatcher                                   │   │
│  │  LISTEN/NOTIFY → agent selection → CLI/API spawn          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Git Integration                              │   │
│  │  hooks + file watcher → live ingest → branch-aware state  │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
         │              │              │
    ┌────┴────┐   ┌─────┴────┐   ┌────┴─────┐
    │Claude   │   │ Gemini   │   │ Codex    │
    │Code     │   │ CLI      │   │ CLI      │
    └─────────┘   └──────────┘   └──────────┘
```

## MCP Server: The Universal Agent Interface

The context store replaces multiple single-purpose MCP servers with one universal server:

| Today (separate servers) | Context Store (one server) |
|--------------------------|---------------------------|
| `filesystem.read_file` | `get_context` — returns structured understanding, not raw text |
| `bash.execute` | `execute` — runs code sandboxed, captures result as nodes |
| `grep.search` | `query` — semantic + structural search, not string matching |
| `git.history` | `history` — temporal edges, finer granularity than commits |

### MCP Tools

| Tool | Purpose |
|------|---------|
| `get_context(intent?, input?)` | Context router: classify → assemble → return curated nodes |
| `execute(node_id?, source?)` | Materialize → compile → sandbox → run → return result |
| `mutate(node_id, changes)` | Update nodes under advisory lock |
| `store_result(nodes, edges)` | Agent writes back what it learned/decided/created |
| `claim_work(step_id, agent_id)` | Advisory lock on a plan step |
| `complete_work(step_id, results)` | Release lock, store results, update status |
| `query(text, project_id?)` | Semantic search + graph traversal across all domains |
| `history(node_id)` | Temporal edge history for a node |

Any MCP-capable tool — Claude Code, Cursor, Windsurf, Gemini, custom agents — connects and immediately gets: project understanding, execution capability, and shared memory with every other connected agent.

### CLI (`ctx`)

| Command | Purpose |
|---------|---------|
| `ctx init` | Start Postgres container, create schema |
| `ctx ingest <path>` | Decompose source files into nodes |
| `ctx watch` | File watcher — live ingest on save |
| `ctx plan <description>` | Spawn agent to create plan, store as nodes |
| `ctx run <plan-id>` | Dispatch plan steps to agents |
| `ctx status` | What's in progress, blocked, done |
| `ctx query <text>` | Semantic + graph search |
| `ctx history <node-id>` | Temporal edge history |
| `ctx branch <name>` | Create isolated branch in the context store |
| `ctx merge <branch>` | Merge branch nodes back to main |
| `ctx prune` | Remove stale/unused nodes based on access patterns |

## Orchestration: The Database Is the Dispatcher

The context store doesn't just serve context — it orchestrates work. The orchestration is implicit: the data tells agents what to do.

### Pull Model (MCP — works today)
Agent starts a session, calls `get_context`. The response includes: project state + "plan step 3 is pending and unclaimed." Agent calls `claim_work(step_3)`. No supervisor needed.

### Push Model (Dispatcher — CLI/API spawn)
LISTEN/NOTIFY fires when a plan step becomes unblocked. A lightweight watcher picks the right agent for the job and spawns it:

```
[DISPATCH] Plan step 3 unblocked (step 2 completed)
[DISPATCH] Intent: code_generation, agent: claude
[CONTEXT]  Assembled 12 nodes, 3 decisions, 1 constraint
[SPAWN]    claude -p "<context>...</context> Implement step 3"
[AGENT]    claude: claimed step 3, writing UserService.cs
[AGENT]    claude: calls store_result(decomposed 4 methods)
[AGENT]    claude: calls complete_work(step_3, {files_changed: 1})
[DISPATCH] Step 3 completed, step 4 unblocked
[DISPATCH] Step 4 is type: review, agent: gemini
[SPAWN]    gemini -p "<context>...</context> Review step 3 output"
```

### Agent Selection
Plan steps carry a `preferred_agent` attribute, or the context router picks based on intent. The dispatcher invokes via CLI (`claude -p`, `gemini -p`, `codex exec`) or API.

### State Machine
Plan steps have statuses: `pending → claimed → in_progress → review → completed`. Transitions trigger actions. Some spawn agents. Some notify humans. Some are fully automated. The database is the event bus, the state machine, and the memory — all in one.

## Execution Engine: Nodes Are Runnable

A node isn't just data — it can be code. `execute(node_id)` materializes the node tree into source, compiles it, runs it in a sandbox, and captures the result as a new node.

### What This Replaces

| Agent does today | With context store |
|-----------------|-------------------|
| `bash("dotnet build")` | `execute(project_root_node)` — tracked, replayable |
| `bash("dotnet test")` | `execute(test_node)` — result stored, queryable |
| Read file to understand structure | `get_context` — already decomposed |
| Write file to make changes | `mutate(node_id)` — granular, audited |

### Execution Results Are Queryable
Because results are nodes, they're searchable:
- "What was the test output last time we changed UserService?" — graph query
- "Show me every execution that failed in the last 3 plan steps" — graph query
- The agent doesn't re-run tests to check — it queries execution history first

### Self-Created Tools
Agents write code, store it as nodes, tag it as reusable. Next time any agent needs similar functionality, pgvector search finds it. The system accumulates capability by being used.

```
Agent: I need to calculate dependency depth
Agent: *searches DB* — finds existing BFS traversal (created by previous agent)
Agent: execute(bfs_node_id, root: "some-id")
MCP:   { "maxDepth": 7, "nodes": [...] }
```

### Sandboxing
The context store is the trust boundary. Agents don't get raw shell access — they get sandboxed execution. The store controls what can run, what can be mutated, what's visible.

Execution options:
- **Roslyn scripting** for expressions/snippets
- **In-memory compilation** (`CSharpCompilation` → `AssemblyLoadContext`) for full classes
- **Temp project + dotnet run** for code with NuGet dependencies

Sandbox constraints: timeout, memory limit, no filesystem access (or restricted temp dir), network deny by default.

### Nodes As External Connectors
A node with type `skill` and attribute `endpoint: https://api.stripe.com/v1/charges` is a live connection to Stripe. A node with `mcp_server: verse-rag` is a bridge to documentation. The node model is the universal adapter — the graph tells you how to reach anything.

## Git Integration: Live Ingest

The context store stays current with the codebase automatically.

### Git Hooks
| Hook | Action |
|------|--------|
| `post-commit` | Diff changed files → decompose → upsert nodes |
| `post-checkout` | Update context to reflect new branch state |
| `post-merge` | Re-ingest merged files, update edges |

### File Watcher (Daemon Mode)
A background process watches the working directory. File saved → decompose → upsert nodes → update embeddings → NOTIFY. The context store is always current, not just at commit boundaries.

```
File saved → watcher → decompose → upsert nodes → update embeddings → NOTIFY
```

### Branch-Aware Context
The context store knows which branch produced which nodes. Switch to `feature/auth` → agent gets auth-related context. Switch to `main` → agent gets main's context. Temporal edges track which branch a node was created on.

### Structural Diffs
Git diffs are file-level. Context store diffs are structural: "method `Add` changed its body" not "line 47 changed." The agent sees what *structurally* changed, not raw unified diff output.

### Initial Setup
`ctx ingest` is the initial import. After that, the watcher (or git hooks) keep it current. Same pattern as `git init` — run once, then tracking is automatic.

## Self-Modification: The System Grows Itself

Because the context store can ingest its own code, execute nodes, and store results, it's a self-modifying runtime.

### Self-Ingestion
Git hook fires → context store's own code streams in as nodes. The store understands its own structure. It can query "what methods does my context router have?" and get a real answer from its own node tree.

### Self-Extension
An agent needs a new capability (e.g., Python parsing). It writes a tree-sitter wrapper, stores it as nodes, tags it as a skill. Now every future agent can `execute` that skill. No deployment, no Docker rebuild.

```
Human: "Add Python support"
Context store:
  → creates branch experiment/python-support
  → spawns Claude: "write a tree-sitter Python decomposer"
  → Claude writes it as nodes, calls execute to test
  → spawns Gemini: "review for correctness"
  → fixes review findings, re-executes, tests pass
  → "experiment/python-support ready for merge"
  → Human: "merge it"
  → nodes merge to main, temporal edges mark transition
  → context store can now decompose Python
```

### Self-Pruning
The context store tracks what gets queried. Nodes that haven't been accessed in N days get demoted: drop the embedding (saves vector storage), keep the skeleton (node type, name, parent). Superseded method versions with closed temporal edges get archived. Usage patterns drive the pruning strategy — the store knows what's relevant because it knows what's been asked for.

Pruning tiers:
| Tier | Criteria | Action |
|------|----------|--------|
| **Hot** | Queried in last 7 days | Full node + embedding + edges |
| **Warm** | Queried in last 30 days | Node + edges, drop embedding |
| **Cold** | Not queried in 30+ days | Skeleton only (type, name, parent) |
| **Archive** | Temporal edge closed, superseded | Move to archive table |

### Branching for Safety
Risky changes happen on branches. `ctx branch experiment/new-decomposer` creates an isolated context. Agents work there, execute there, test there. If it works, merge. If not, drop the branch. Same mental model as git, but for structured data. Temporal edges support this naturally — `valid_from` on the branch, `valid_to` when merged or abandoned.

## Product Split

| | Free (CLI) | Paid (Hosted) |
|--|------------|---------------|
| Runtime | Local Docker | Managed Postgres |
| Agents | Local CLI (claude, gemini, codex) | API dispatch |
| UI | Terminal output | Web dashboard (chat UI already exists) |
| Data | Your machine | Cloud (team-shared) |
| Multi-user | No | Yes |
| Execution | Local sandbox | Managed sandbox |
| Git integration | Local hooks + watcher | Webhooks from GitHub/GitLab |

The free CLI is the product for individual devs running AI agents locally. They already have Docker and CLI tools. The value proposition: **your agents remember what they learned and coordinate without you babysitting them.**

## What Exists Today (in CodeStoragePoc)

- Node model with 20+ types across code/plan/conversation/idea domains ✓
- Context router (intent classification + assembly + prompt building) ✓
- Temporal edges in AGE (valid_from/valid_to/provenance) ✓
- Advisory locks for concurrent agents (SubtreeLock) ✓
- LISTEN/NOTIFY for reactive triggers (ChangeNotifier) ✓
- Plan-to-code generation with temporal tracking ✓
- Chat UI with multi-model SSE streaming ✓
- MCP servers (dev, skills) ✓
- Roslyn decomposer (in db-ast-poc) ✓
- CSharpGenerator (node tree → compilable source) ✓
- RoslynValidator (parse-level validation) ✓
- DotnetBuilder (build wrapper) ✓

## What Needs Building

| Priority | Component | What It Does |
|----------|-----------|-------------|
| **1** | `agent-context` MCP server | Core interface — wraps context router + node CRUD + locking |
| **2** | `ctx ingest` CLI | Decompose source files into nodes (initial import) |
| **3** | Token measurement harness | Before/after comparison proving savings |
| **4** | `execute` tool | Materialize → compile → sandbox → run → capture |
| **5** | Git hooks + file watcher | Live ingest, branch-aware context |
| **6** | Dispatcher | LISTEN/NOTIFY → agent selection → CLI spawn |
| **7** | Self-pruning | Access tracking + tiered demotion |
| **8** | Branch model | Isolated context branches with merge |

## Proving It

The key claim is: **agents with the context store use fewer tokens and produce better results than agents starting cold.** This is measurable:

1. Take a real task on a real project
2. Run it with Claude Code cold (no context store) — measure tokens, time, file reads
3. Run same task with context store — measure same metrics
4. Compare

If the context store doesn't measurably reduce token usage on a 20+ file project, the premise is wrong and we should stop.

## Relationship to Existing Work

- **006 (Agent Executable Context)** — subset of this vision. Execution is one capability among many.
- **db-ast-poc** — has the best code decomposer. Port as the `ctx ingest` backend for C#.
- **Context router** — already built, becomes the core of `get_context`.
- **orchestration-3090** — early dispatcher concept. Replace with DB-driven dispatch.
- **skills.json / SkillLoader** — static version of what self-created tools do dynamically.
- **CLAUDE.md / MEMORY.md** — the flat-file predecessors that the context store replaces.
