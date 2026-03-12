# 006 — Agent Executable Context

## Core Idea

The database isn't just storage — it's a **runtime**. Agents connect via MCP and can write, compose, and execute code on demand. No file system needed. The DB is the agent's workspace, scratch pad, and growing toolbox.

## Three Key Shifts

### 1. Code Execution as an MCP Tool

Agents get an `execute` tool alongside read/write/search. The loop becomes:

```
Think → Mutate nodes → Materialize → Execute → Observe → Repeat
```

MCP tools:

| Tool | Purpose |
|------|---------|
| `get_node` | Read node + children |
| `mutate_node` | Insert/update/delete nodes |
| `execute` | Materialize → compile → run → return stdout/stderr |
| `query_graph` | Cypher queries (CALLS, REFERENCES, DEPENDS_ON) |
| `search` | pgvector similarity across all nodes |

The agent doesn't need shell access. `execute` is sandboxed: DB → Roslyn compile → run in isolated AssemblyLoadContext → return result.

### 2. Agents Create Their Own Tools

The agent isn't just editing *your* code. It writes and runs its own code as a thinking tool.

- Need to parse CSV? Write a parser, execute it, get structured data.
- Need to validate a formula? Write a test, run it.
- Need a graph traversal? Write BFS, execute it, tag it as reusable.

These self-created tools accumulate in the DB. Next time *any* agent needs similar functionality, pgvector search finds the existing one. The system gets smarter by being used.

```
Agent: I need to calculate dependency depth
Agent: *searches DB* — nothing
Agent: *writes BFS traversal as nodes*
Agent: execute(bfs_id, root: "some-id")
MCP:   { "maxDepth": 7, "nodes": [...] }
Agent: *tags as reusable skill*
```

### 3. Context Is a Query, Not a File

Today: compilation unit = file (1:1 mapping).

Proposed: compilation unit = **any set of nodes assembled by query**.

The agent's executable context is composed dynamically:
- Pull method A from project X
- Pull struct B from project Y
- Pull a utility it wrote last week
- Synthesize into one compilation unit
- Execute

The materializer works from a node set, not a file ID:

```csharp
// today
GenerateFromFile(fileId)

// proposed
GenerateFromNodes(IEnumerable<NodeRef> nodes)
```

Dependencies resolved by graph traversal (DEPENDS_ON, REFERENCES edges). Imports derived from what's actually used. The compiler doesn't care where the nodes came from.

| Old Mental Model | New Mental Model |
|-----------------|-----------------|
| File | Node query result |
| Project | Subgraph |
| Import statement | Graph edge |
| Dependency | DEPENDS_ON traversal |
| File system | Database |

This mirrors what the context router already does for prompts — assembling per-intent context from DB queries. Same pattern, but for executable code instead of LLM prompts.

## Advantages Over File-Based Agents

- **Audit trail** — every mutation is tracked (temporal edges in db-ast-poc)
- **Granular edits** — change one method body without touching the file
- **Blast radius awareness** — query CALLS/REFERENCES before changing anything
- **Trivial rollback** — revert node state, not git diffs
- **Concurrent agents** — advisory locks on subtrees (SubtreeLock already exists)
- **Accumulated capability** — agents build up a searchable library of tools they wrote
- **Any MCP client works** — Claude Code, Gemini, Codex, custom UI

## Execution Engine Options

| Option | Mechanism | Best For |
|--------|-----------|----------|
| **Roslyn Scripting** | `CSharpScript.EvaluateAsync(source)` | Snippets, expressions |
| **In-memory compilation** | `CSharpCompilation` → `AssemblyLoadContext` → reflection | Full classes, multi-method |
| **Temp project + dotnet run** | Materialize to disk, build, execute | Full projects with dependencies |

**Recommended**: Option B (in-memory compilation) for the common case. Option C as fallback for code that needs NuGet packages.

## Sandboxing Considerations

- `AssemblyLoadContext` provides isolation — unload after execution
- No file system access from executed code (or restricted to a temp dir)
- Timeout on execution (prevent infinite loops)
- Memory limits (prevent OOM)
- Network access policy (deny by default?)
- Output capture: redirect stdout/stderr to string buffers

## Open Questions

1. **How does the agent specify what to execute?** Entry point — method name + args? Expression? Test assertion?
2. **How are NuGet dependencies handled?** Pre-compiled reference assemblies? On-demand restore?
3. **State between executions** — does each run start fresh, or can the agent build up state across calls?
4. **Multi-language** — this assumes C# but the node model is language-agnostic. Could the same pattern work with Python/JS materializers?
5. **How does the agent discover its own previously-created tools?** pgvector search by description? A dedicated "agent toolbox" node type?

## Replayable Agent Conversations

Every agent action becomes a node. The conversation isn't a log file — it's a queryable graph.

### Node Structure

```
turn (agent: claude, intent: create_tool)
├── mutate_node (created BFS traversal)
├── execute (ran it, got result)
│   └── tool_result (stdout: "maxDepth: 7")
├── mutate_node (fixed off-by-one)
├── execute (ran again)
│   └── tool_result (stdout: "maxDepth: 6")
└── turn (agent: claude, target: user, "here's the analysis")
```

### What This Enables

- **Replay** — walk the tree and re-execute every step
- **Fork** — "what if the agent took a different approach at step 3?" Branch the subgraph
- **Cross-agent comparison** — same task, three models, compare the node trees they produced
- **Debug** — "why did the agent make this change?" Follow edges back to the triggering turn
- **Benchmark** — identical context, different models, measurable outcomes

### Queryable via Cypher

```cypher
-- Every time Claude wrote code, ran it, and failed
MATCH (t:turn)-[:PRODUCED]->(m:mutate_node)-[:TRIGGERED]->(e:execute)
WHERE t.agent = 'claude' AND e.exit_code <> 0
RETURN t, m, e

-- What spawned from a specific conversation?
MATCH (conv:conversation)-[:CONTAINS]->(t:turn)-[:SPAWNED]->(n)
WHERE conv.id = $convId
RETURN n.type, n.name, count(*)
```

### Edge Types for Agent Actions

| Edge | From | To | Meaning |
|------|------|----|---------|
| `PRODUCED` | turn | mutate_node | Agent action created this mutation |
| `TRIGGERED` | mutate_node | execute | Mutation led to execution |
| `FORKED_FROM` | turn | turn | Alternative approach branched here |
| `OBSERVED` | execute | tool_result | Execution produced this output |
| `INFORMED` | tool_result | turn | Result influenced next agent decision |

The `route_to_model` skill already creates turns with `target_agent`. Extend so every MCP tool call also creates a node, and the full graph emerges automatically.

## Self-Hosted Roadmap & Visualization

The system should manage its own development. Research docs, plans, ideas, and conversations are all nodes — visualize them together.

### What Exists Today
- Plans 001-005 seeded as node trees in DB
- PlannerPanel shows individual plan trees
- ThreadsSidebar tracks ideas by status
- Research docs 001-006 on disk (NOT in DB)

### What's Missing
- Research docs aren't nodes — they should be, linked to the plans they inform
- No cross-plan view — can't see how plans relate to each other
- No roadmap visualization — timeline, dependencies, status rollup
- Ideas from conversations not linked to plans they feed

### Proposed Roadmap Node Structure

```
roadmap (root node)
├── plan 001: Voice Ideation
│   └──DEPENDS_ON──► plan 004: Test App
├── plan 002: Ideation Nodes
│   └──IMPLEMENTED_BY──► context router (code nodes)
├── plan 003: Review Fixes ✓
├── research 006: Agent Executable Context
│   ├──RELATES_TO──► db-ast-poc temporal edges
│   ├──SPAWNED_FROM──► conversation (this session)
│   └── open questions (queryable nodes)
└── next: Execution Engine
    └──DEPENDS_ON──► research 006
```

### Research as Nodes

Each research doc becomes a subtree:

| Node Type | Purpose | Example |
|-----------|---------|---------|
| `research` | Root of a research doc | "006 Agent Executable Context" |
| `finding` | Key insight or conclusion | "Context is a query not a file" |
| `open_question` | Unresolved question | "How are NuGet deps handled?" |
| `decision` | Committed design choice | "Use in-memory Roslyn compilation" |
| `reference` | Link to external work | "db-ast-poc temporal edges" |

Edges connect research to plans (`INFORMS`), to code (`IMPLEMENTED_BY`), and to conversations (`SPAWNED_FROM`).

### Visualization (extend existing UI)

- **Roadmap tab** in PlannerPanel — DAG view of all plans + research + dependencies
- **Status rollup** — plan status aggregated from child task statuses
- **Timeline** — target_date attributes on milestones, rendered as swimlanes
- **Search** — pgvector across all node types: "what do we know about sandboxing?"
- **Conversation links** — click a node, see the conversations that spawned it

This is the system managing itself. Every conversation like this one produces nodes that feed the roadmap that drives the next conversation.

## Relationship to Existing Work

- **Context router** already assembles dynamic context from DB queries — this extends the pattern to executable code
- **CSharpGenerator** already materializes node trees — needs `GenerateFromNodes(nodeSet)` variant
- **SubtreeLock** already handles concurrent agent access
- **db-ast-poc temporal edges** provide the audit trail for agent mutations
- **skills.json / SkillLoader** is the static version of what this would do dynamically
