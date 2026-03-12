# CodeStoragePoc — Design Decisions

## 1. Trigger vs Application-Layer AGE Sync

**Decision: Application-layer sync.**

AGE Cypher queries require `LOAD 'age'` and `SET search_path` per session. PostgreSQL triggers execute in the server's session context, which doesn't have these loaded. You'd need a C trigger function or a complex workaround to call `cypher()` from a PL/pgSQL trigger.

Application-layer sync is:
- **Simpler**: No cross-extension dependency in the DB
- **Batchable**: We can sync many nodes in one pass after a bulk operation
- **Controllable**: We decide when and what to sync, avoiding graph updates during intermediate states

The tradeoff is **eventual consistency** — the graph may lag behind the relational data briefly. For agent workflows where mutations happen in claimed subtrees, this is acceptable because:
- Only the claiming agent modifies nodes
- Graph sync happens after the mutation transaction commits
- Other agents can't read stale graph edges because they can't claim the same subtree

If strong consistency were required, a `AFTER INSERT` trigger using `dblink` to a secondary session (with AGE loaded) would work, but adds operational complexity.

## 2. Sibling Order During Concurrent Inserts

**Decision: Gap numbering with `FOR UPDATE` on the parent.**

Sibling orders use increments of 100 (100, 200, 300...) to allow insertions between existing children without renumbering. The `NextSiblingOrder` method uses:

```sql
SELECT COALESCE(MAX(sibling_order), 0) + 100
FROM nodes WHERE parent_id = @parentId
FOR UPDATE
```

The `FOR UPDATE` ensures that two concurrent agents trying to add children to the same parent will serialize — the second agent's query blocks until the first transaction commits.

For insertions *between* existing children (e.g., adding a parameter at order 150 between 100 and 200), the caller specifies the order directly. If gaps are exhausted, a renumber operation would be needed, but with gaps of 100, this is unlikely in practice.

Alternative considered: **Fractional ordering** (floats). Rejected because float precision issues accumulate over many insertions, and integer ordering is simpler to reason about.

## 3. Node Attributes: Separate Table vs JSONB

**Decision: Separate `node_attributes` table.**

Advantages of the separate table:
- **Queryable**: Can index and query individual attributes across all nodes (e.g., "find all nodes with access=public")
- **Updatable**: Can update a single attribute without rewriting the entire JSONB blob
- **Schema-visible**: The key-value structure is inspectable via standard SQL tools

Advantages of JSONB (rejected for POC):
- Fewer joins — attributes inline with the node row
- Slightly faster reads when you always need all attributes
- More flexible for nested attribute values

For a production system, **JSONB is likely better** because:
- Attribute reads almost always fetch all attributes for a node
- The separate table creates N+1 query patterns without careful batching
- JSONB with GIN indexing provides equivalent query capability

The POC uses the separate table because it makes the data model more explicit and easier to inspect during development. Migration to JSONB would be straightforward.

## 4. Advisory Lock Key from UUID

**Decision: XOR the two 64-bit halves.**

```csharp
static long UuidToLockKey(Guid id)
{
    var bytes = id.ToByteArray();
    long high = BitConverter.ToInt64(bytes, 0);
    long low = BitConverter.ToInt64(bytes, 8);
    return high ^ low;
}
```

PostgreSQL advisory locks take a `bigint` (64-bit) key. UUIDs are 128-bit. XOR preserves entropy from both halves — every bit of the UUID influences the key.

**Collision probability**: For N concurrent locks, collision probability is ~N²/2⁶⁴. With 1000 concurrent agents, that's ~5×10⁻¹³. Negligible.

**Alternative considered**: Truncation (take first 8 bytes). Rejected because it discards half the UUID's entropy. With UUID v4, both halves carry randomness, so XOR is strictly better.

**Alternative considered**: Hash (SHA-256 → take 8 bytes). Works but adds a crypto dependency for zero practical benefit at this scale.

## 5. Schema Migration for Node Types

**Decision: Node types are `TEXT`, not an enum. No migration needed.**

The `node_type` column is `TEXT NOT NULL`, not a PostgreSQL `ENUM`. New node types are just new string values — no `ALTER TYPE ... ADD VALUE` required.

Validation happens at the application layer:
- The `CSharpGenerator` has a `switch` on node_type — unknown types emit a comment
- The seeder uses known constants
- A future schema version could add a `CHECK` constraint or a lookup table

This is intentional for the POC. In production, the options would be:
1. **Lookup table** (`node_types` with valid types) — provides referential integrity
2. **Application enum** mapped to text — C# enum ensures compile-time safety
3. **PostgreSQL ENUM** — strongest DB-level guarantee but painful to migrate

Option 2 is recommended for production: the C# code already defines which types it supports, and unknown types are handled gracefully.

## 6. Unified Node Model — Code, Plans, and Ideas in One Table

**Decision: Everything is a node.**

Code, plans, conversations, and ideas all share the same `nodes` table. The `node_type` column distinguishes them. The `projects` table separates domains (one project for code, one for plans, one for voice sessions), but the AGE graph connects across projects.

Why this works:
- **Single query language**: All traversal, similarity search, and relationship queries use the same SQL/Cypher patterns
- **Cross-domain edges**: An idea can have an `IMPLEMENTED_BY` edge to a plan_step, which has a `PRODUCES` edge to a method. One Cypher query traces the full lineage: voice conversation → idea → plan → code.
- **pgvector across domains**: Semantic similarity search can find code related to an idea, or ideas related to code (as long as embedding dimensions match per project)
- **Existing infrastructure**: Advisory locks, LISTEN/NOTIFY, gap numbering, recursive CTE subtree fetching — all work unchanged for plans and conversations

The alternative — separate tables per domain — would require:
- Duplicate CRUD code for each domain
- Polymorphic graph references (which AGE doesn't natively support)
- Separate embedding indexes per table
- Different traversal queries per domain

The tradeoff is **node type sprawl** — 20+ types in one table. Validation happens at the application layer (renderers handle known types, emit comments for unknown). A future `node_types` lookup table could enforce referential integrity if needed.

### Domain Renderers

Each domain gets its own renderer that walks the node tree and emits domain-specific output:
- **CSharpGenerator**: code nodes → `.cs` source files
- **PlanRenderer**: plan nodes → readable text with status indicators
- **ConversationRenderer**: conversation nodes → threaded transcript with speaker/mode badges

The renderer pattern is: fetch subtree via recursive CTE → walk depth-first → switch on node_type → emit formatted output. Unknown types are handled gracefully.

## 7. Plans as First-Class Data

**Decision: Plans are stored in the database, not just in markdown files.**

The markdown backup (`plans/*.md`) exists as a safety net and for human review outside the system. The database is the source of truth. This enables:

- **Status tracking**: Each plan_step and task has a `status` attribute that updates as work progresses
- **Graph integration**: Plan steps can have `PRODUCES` edges to the code nodes they create
- **Cross-plan queries**: "Show me all in-progress steps across all plans" is one SQL query
- **Retrospectives**: Stored as nodes, linked to the plan they evaluate
- **Audit trail**: `modified_by` tracks which agent touched each node, `modified_at` tracks when

The plan node hierarchy mirrors the XML plan format from the project conventions:
```
plan
├── plan_phase (GATHER, PLAN, APPROVE, EXECUTE, CLOSE_OUT)
│   ├── plan_step (numbered implementation steps)
│   │   └── task (sub-tasks within a step)
│   ├── test_spec (verification items)
│   └── retrospective (filled at close-out)
├── risk (things that could go wrong)
└── question (open decisions with proposed answers)
```

## 8. Connection Pattern for Lock-Safe Mutations

**Decision: NodeRepository factory pattern with optional connection injection.**

The critical bug in the original POC: `SubtreeLock` holds an advisory lock on one connection, but `NodeRepository` opens a *new* connection per operation. Mutations happen outside the lock — another agent could interleave.

**Fix**: NodeRepository accepts an optional `NpgsqlConnection` + `NpgsqlTransaction?` in its constructor:
- **No connection provided** (default): opens a new connection per operation. Safe for reads.
- **Connection provided** (from `SubtreeLock.LockConnection`): all queries use that connection. Mutations are protected by the advisory lock.

```csharp
// Read-only operations — own connections
var repo = new NodeRepository(connStr);

// Lock-safe mutations — use the lock's connection
await using var lockMgr = new SubtreeLock(connStr);
await lockMgr.TryClaimSubtree(nodeId, agentId);
var lockedRepo = new NodeRepository(lockMgr.LockConnection!);
await lockedRepo.InsertNode(...);  // runs on the lock session
```

**Alternative considered**: Method overloads (`InsertNode(...)` and `InsertNode(conn, tx, ...)`). Rejected because it doubles the API surface and callers must remember which overload to use. The factory pattern makes it impossible to accidentally use the wrong connection.

**Related fix**: `InsertNode` now wraps node + attributes in a transaction. If attribute insertion fails, the node insert is rolled back — no "naked" nodes.

## 9. Embedding Dimensions: 768 (nomic-embed-text)

**Decision: Standardize on 768 dimensions for all embeddings.**

The original POC used `vector(1536)` (matching OpenAI's text-embedding-ada-002). Phase 1 migrated to `vector(768)` to match `nomic-embed-text` via Ollama — the local-first embedding model.

Key considerations:
- **Local-first**: No API keys or external dependencies for embedding
- **Prefix-aware**: nomic-embed-text uses `search_document:` and `search_query:` prefixes for better retrieval quality
- **768 vs 1536**: Smaller vectors mean faster cosine distance, smaller index, less memory. Quality is comparable for code/plan retrieval at this scale.

The schema migration requires `docker compose down -v` (drop volumes) since `CREATE TABLE IF NOT EXISTS` won't alter an existing column's dimension.

## 10. Temporal Edges in AGE

**Decision: Edge properties (`valid_from`, `valid_to`, `provenance`) on AGE edges, not separate tables.**

When a plan task gets modified and code is regenerated, we need to know which version of the task produced which version of the code. Two approaches:

1. **Separate `edge_versions` table** — relational tracking of edge validity periods
2. **Edge properties in AGE** — `valid_from`, `valid_to`, `provenance` on the Cypher edge itself

We chose option 2 because:
- **Single query language**: `MATCH (a)-[e:IMPLEMENTED_BY]->(b) WHERE e.valid_to IS NULL` gives current edges; removing the WHERE gives full history
- **Atomic with edge creation**: No cross-system consistency problem between AGE and a relational table
- **AGE supports it**: Confirmed via live testing that `WHERE e.valid_to IS NULL` correctly matches edges where the property was never set (AGE treats missing properties as NULL)

**Close-before-create pattern**: On regeneration, `CloseAllEdgesFrom(taskId, "IMPLEMENTED_BY")` sets `valid_to` on all open edges from a task, then `CreateTemporalEdge` creates new edges with a fresh `valid_from`. Old edges persist as historical records.

**Verified behavior**: After mutating a plan task and regenerating, `QueryTemporalEdges` returns both the closed (old) and open (current) edges with their timestamps — proving version history works.

## 11. Cross-Project AGE Edges

**Decision: Sync vertices for BOTH projects before creating cross-project edges.**

`IMPLEMENTED_BY` edges connect plan tasks (in planProjectId) to code methods (in projectId). AGE `MATCH` requires both endpoints to exist as vertices. Since `SyncAllVertices` syncs one project at a time, we must call it for both projects before creating edges.

**Bug found and fixed during implementation**: `GenerateFromPlan` initially only synced the code project's vertices. Plan task vertices didn't exist in AGE, so `MATCH` found nothing and 0 edges were created. Fix: sync plan project first, then code project.

This is a general constraint for any cross-project edge: both endpoint projects must be vertex-synced before edge creation. The code documents this:

```csharp
// Sync BOTH plan and code vertices — tasks live in the plan project,
// code nodes live in the code project. Both must be AGE vertices
// before we can create edges between them.
await _age.SyncAllVertices(_repo, planProjectId);
await _age.SyncAllVertices(_repo, projectId);
```

## 12. Plan-to-Code Generation: Structured Attributes, Not NLP

**Decision: Task nodes carry explicit code-gen attributes. No free-text parsing.**

Each task in a code-generation plan has structured attributes: `method_name`, `return_type`, `params`, `body`. The plan root carries `target_namespace` and `target_class`. PlanToCodeGenerator reads these directly — it never parses natural language descriptions.

Why:
- **Deterministic**: Same plan always produces same code nodes. No LLM variance.
- **Testable**: Attribute presence is a simple dictionary lookup. No regex/NLP fragility.
- **Composable**: An LLM can populate the structured attributes upstream, then the generator runs without LLM involvement.

The `params` attribute uses a simple format (`"int a, int b"`) parsed by comma-split + space-split. This is intentionally minimal — production would use Roslyn's `SyntaxFactory` for proper parameter parsing.
