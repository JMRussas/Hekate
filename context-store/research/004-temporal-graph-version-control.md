# Research 004: Temporal Graph Version Control

## Status: Concept (not yet planned)

## Core Idea

Instead of storing snapshots of files (like git does with blobs), store **time-bounded node states**. Every node has a validity interval. A "commit" is a timestamp that groups a set of node mutations. Branching is a graph concept, not a filesystem copy.

## Three Primitives

### 1. Temporal Nodes — `valid_from` / `valid_to`

```sql
ALTER TABLE nodes ADD COLUMN valid_from TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE nodes ADD COLUMN valid_to   TIMESTAMPTZ; -- NULL = current version
```

"Updating" a node = close old row + insert new row. Query at any point in time:

```sql
SELECT * FROM nodes
WHERE valid_from <= @timestamp AND (valid_to IS NULL OR valid_to > @timestamp);
```

Existing `GetSubtree` recursive CTE adds one WHERE clause and works at any historical point.

### 2. Commits — A Timestamp + Metadata Node

```sql
-- A commit is just another node type
INSERT INTO nodes (node_type, name, value, parent_id)
VALUES ('commit', 'Fix Add method signature', 'agent-001', @branch_node_id);

-- Each mutation within a commit references it
ALTER TABLE nodes ADD COLUMN commit_id UUID REFERENCES nodes(id);
```

Existing `modified_by` and `modified_at` are already halfway there.

### 3. Branches — Graph Topology in AGE

Branches are zero-cost — no data copying. A branch is a pointer (HEAD edge) to a commit node. Every commit knows its parent(s). Merges have two parents.

```cypher
CREATE (b:Branch {name: 'main'})
CREATE (c1:Commit {id: '...', message: '...', timestamp: '...'})
CREATE (b)-[:HEAD]->(c3)
CREATE (c1)-[:PARENT]->(c2)-[:PARENT]->(c3)

// Branching = new Branch node pointing to same commit
CREATE (fb:Branch {name: 'feature/add-scalar'})
CREATE (fb)-[:BRANCHED_FROM]->(c2)
CREATE (fb)-[:HEAD]->(c5)
```

## Integration With Existing Stack

| Existing Feature | Version Control Extension |
|---|---|
| `nodes` table | Add `valid_from`, `valid_to`, `commit_id` |
| `modified_by`, `modified_at` | Becomes commit metadata |
| AGE graph | Branch/commit topology, `PARENT`, `HEAD`, `BRANCHED_FROM` edges |
| Recursive CTE subtree fetch | Add temporal filter — "show me the code at commit X" |
| pgvector embeddings | Embed commit diffs for "find commits related to this bug" |
| Advisory locks (SubtreeLock) | Same pattern — lock subtree before mutating on a branch |
| `LISTEN/NOTIFY` | Notify on commits, not individual mutations |

## Structural Diff (Not Line Diff)

Git diffs files line-by-line. This system diffs **node trees structurally**:

```sql
SELECT n_new.id, n_new.node_type, n_new.name,
       n_old.value AS old_value, n_new.value AS new_value
FROM nodes n_new
JOIN nodes n_old ON n_old.id = n_new.id
WHERE n_new.commit_id = @commitB
  AND n_old.valid_to = n_new.valid_from;
```

Semantic diffs: "the return statement in the Add method changed" rather than "line 47 changed."

## Merge = Graph Merge (Three-Way)

1. Find **common ancestor commit** (Cypher: shortest path backward via PARENT edges)
2. Get node tree at ancestor, branch A, branch B (three temporal queries)
3. Compare structurally:
   - Changed only in A → take A
   - Changed only in B → take B
   - Changed in both → conflict (node-level, not line-level)
4. Advisory locks prevent conflicts within a branch (agents claim subtrees)

## Estimated Implementation Cost

| Component | Work | Lines |
|---|---|---|
| Mutation wrapper (temporal rows instead of in-place update) | NodeCrud changes | ~50 |
| Commit manager (open/collect/close) | New class | ~100 |
| Temporal query layer (`GetSubtreeAt(nodeId, timestamp)`) | Per-method variants | ~30 each |
| Branch/commit AGE topology | New vertex/edge labels | ~150 |
| Garbage collection (prune old versions) | Cron job | ~50 |

Generators and renderers don't change — they take a `TreeNode` and render it regardless of when it came from.

## Relationship to db-ast-poc

The db-ast-poc project may be exploring similar or adjacent patterns. Cross-reference its DB schema and approach before planning implementation.
