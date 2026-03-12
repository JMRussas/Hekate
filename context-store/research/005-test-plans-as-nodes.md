# Research 005 — Test Plans as Nodes

**Date:** 2026-03-09
**Status:** Research / proposal
**Context:** CodeStoragePoc stores code, plans, conversations, and ideas as nodes in one table. This document explores how test plans — structured collections of test cases with execution state — fit the same model.

## 1. Node Type Mapping

The existing model already has `test_spec` (verification criteria inside plans). Test plans need a richer hierarchy: suites contain cases, cases contain steps. Here's how each concept maps.

### New Node Types

| Concept | Node Type | Name | Value | Parent |
|---------|-----------|------|-------|--------|
| Test suite | `test_suite` | Suite name (e.g., "Smoke Tests — Context Router") | Description of what the suite covers | Project root or another suite (nesting) |
| Test case | `test_case` | Case name (e.g., "T-001: Ideation intent classifies correctly") | Preconditions or setup description | `test_suite` |
| Test step | `test_step` | Step label (e.g., "Send ideation message") | The action to perform or assertion to check | `test_case` |

### Why Not Reuse `test_spec`?

`test_spec` is a lightweight verification criteria node inside plans — it says *what* to verify, not *how*. A `test_case` is richer: it has steps, execution history, priority, and a runnable command. The two serve different purposes and connect via edges (see Section 2).

Think of `test_spec` as the requirement and `test_case` as the implementation:

```
plan_step "Context Router Service"
└── test_spec "Intent classifier returns correct intent for ideation input"
        ↓ VERIFIED_BY (edge)
    test_case "T-001: Ideation intent classification"
    ├── test_step "Send '@haiku I have an idea for voice input'"
    ├── test_step "Assert intent = 'ideation'"
    └── test_step "Assert context includes open threads"
```

### Expected Result — Attribute, Not Child Node

Expected results belong as attributes on `test_step` nodes, not as separate child nodes. Reasons:

- An expected result is a property of a step, not an independent entity.
- It doesn't need its own graph edges, embedding, or lifecycle tracking.
- The `node_attributes` table already handles key-value pairs per node.

A `test_step` with an assertion would look like:

```
node_type: test_step
name: "Assert intent = 'ideation'"
value: "Check classifier output matches expected intent"
attributes:
  expected_result: "ideation"
  assertion_type: "equals"
```

For complex expected outputs (multi-line JSON, file diffs), `value` on the step node holds the full expected output, and `expected_result` holds a summary or hash.

### Priority and Type — Attributes on `test_case`

These are properties of a test case, not structural elements:

```
attributes:
  priority: "p0"           # p0 / p1 / p2
  test_type: "smoke"       # smoke / regression / stress / edge_case / integration / unit
```

This matches how plans use attributes (`status`, `severity`, `plan_type`) rather than child nodes for metadata.

### Node Hierarchy Summary

```
test_suite "Smoke Tests — Context Router"
├── test_case "T-001: Ideation intent classification"      [p0, smoke]
│   ├── test_step "Send ideation message"
│   ├── test_step "Assert intent = 'ideation'"
│   └── test_step "Assert context includes open threads"
├── test_case "T-002: Parking intent bypasses model"       [p0, smoke]
│   ├── test_step "Send 'park this thread'"
│   ├── test_step "Assert no model call"
│   └── test_step "Assert thread status = 'parked'"
└── test_case "T-003: Plan step produces code node"        [p1, integration]
    ├── test_step "Create plan with EXECUTE step"
    ├── test_step "Execute step → generate code"
    └── test_step "Assert PRODUCES edge exists"
```

## 2. Edge Labels

### New Edge Labels

| Edge | From | To | Meaning |
|------|------|----|---------|
| `VERIFIES` | `test_case` | code node (method, class, etc.) | This test verifies that code node's behavior |
| `VERIFIED_BY` | `test_spec` | `test_case` | This plan spec is implemented by this test case |
| `REQUIRES` | `test_case` | `test_case` | T-002 requires T-001 to pass first (ordering dependency) |
| `GUARDS` | `test_case` | any node | Regression test — this test exists because of a bug in that node |

### Why These Four

**`VERIFIES`** — The fundamental link between tests and code. Without it, you can't answer "what tests cover this method?" This is the test-plan equivalent of `PRODUCES` in the plan domain.

**`VERIFIED_BY`** — Connects plan specs to their implementation as test cases. Bidirectional with `VERIFIES` but targeting different source types. A `test_spec` says "this should be tested"; `VERIFIED_BY` points to the actual test.

**`REQUIRES`** — Test ordering. Some tests are prerequisites (e.g., "DB connection works" must pass before "query returns correct results"). Without explicit ordering, a test runner can't intelligently short-circuit on failure.

**`GUARDS`** — Regression provenance. When a bug is found and a test is written to prevent recurrence, `GUARDS` links the test to the node where the bug lived. This answers "why does this test exist?" long after the bug is forgotten.

### Reusing Existing Edge Labels

Some existing edges apply naturally:

| Edge | Usage in Test Domain |
|------|---------------------|
| `DEPENDS_ON` | Test suite depends on a module or service being available |
| `RELATES_TO` | Test case relates to a plan step or idea (weaker than `VERIFIES`) |
| `BLOCKS` | Failing test blocks a plan step from completing |

### Edge Registration

Add to `AgeLayer.ValidEdgeTypes`:

```csharp
// Test domain
"VERIFIES", "VERIFIED_BY", "REQUIRES", "GUARDS"
```

No schema changes needed — AGE creates edge labels dynamically via `CREATE (a)-[:VERIFIES]->(b)`.

## 3. Attribute Schema

### `test_suite` Attributes

| Key | Values | Purpose |
|-----|--------|---------|
| `status` | `active` / `archived` / `draft` | Suite lifecycle |
| `run_command` | Shell command | Run all tests in this suite |
| `last_run` | ISO timestamp | When the suite was last executed |
| `pass_rate` | Percentage string (e.g., "95.0") | Latest pass rate |

### `test_case` Attributes

| Key | Values | Purpose |
|-----|--------|---------|
| `status` | `pending` / `passing` / `failing` / `skipped` / `flaky` | Current test status |
| `priority` | `p0` / `p1` / `p2` | Execution priority |
| `test_type` | `smoke` / `regression` / `stress` / `edge_case` / `integration` / `unit` | Classification |
| `command` | Shell command or function reference | How to run this specific test |
| `expected_output` | String or pattern | What success looks like (for simple cases) |
| `last_run` | ISO timestamp | Last execution time |
| `last_result` | `pass` / `fail` / `error` / `skip` | Outcome of last run |
| `duration_ms` | Integer as string | How long the last run took |
| `fail_count` | Integer as string | Consecutive failure count (flaky detection) |
| `introduced_by` | Agent name or commit hash | Who wrote this test |
| `bug_ref` | Description or ID | For regression tests — what bug this guards against |

### `test_step` Attributes

| Key | Values | Purpose |
|-----|--------|---------|
| `step_type` | `action` / `assertion` / `setup` / `teardown` | What kind of step |
| `expected_result` | String | Expected outcome of an assertion step |
| `assertion_type` | `equals` / `contains` / `matches` / `not_null` / `truthy` | How to compare |
| `actual_result` | String | Last actual result (populated after execution) |
| `status` | `pending` / `passed` / `failed` | Step-level result |

## 4. Query Patterns

### Q1: "Give me all smoke tests for module X"

Find all test cases classified as smoke tests that verify any code node under a given module.

**SQL + Cypher (two-step):**

```sql
-- Step 1: Find code node IDs under module X via recursive CTE
WITH RECURSIVE module_nodes AS (
    SELECT id FROM nodes WHERE name = 'ContextRouter' AND node_type = 'namespace'
    UNION ALL
    SELECT n.id FROM nodes n JOIN module_nodes m ON n.parent_id = m.id
)
SELECT id FROM module_nodes;
```

```cypher
-- Step 2: Find test_case nodes that VERIFIES any of those code nodes
MATCH (t:CodeNode)-[:VERIFIES]->(c:CodeNode)
WHERE c.node_id IN ['<id1>', '<id2>', '...']
AND t.node_type = 'test_case'
RETURN t.node_id, t.name
```

```sql
-- Step 3: Filter to smoke tests via attributes
SELECT n.id, n.name, n.value
FROM nodes n
JOIN node_attributes a ON a.node_id = n.id
WHERE n.id = ANY(@test_node_ids)
AND a.key = 'test_type' AND a.value = 'smoke';
```

**Pure SQL alternative** (no Cypher, uses node_attributes for the VERIFIES link stored as an attribute fallback):

```sql
-- If VERIFIES relationships are also tracked as attributes (verifies_node_id)
SELECT tc.id, tc.name, tc.value,
       pri.value AS priority, tt.value AS test_type
FROM nodes tc
JOIN node_attributes tt ON tt.node_id = tc.id AND tt.key = 'test_type'
LEFT JOIN node_attributes pri ON pri.node_id = tc.id AND pri.key = 'priority'
WHERE tc.node_type = 'test_case'
AND tt.value = 'smoke'
AND tc.id IN (
    -- test cases that verify nodes under module X (via AGE or a verifies_target attribute)
    SELECT a.node_id FROM node_attributes a
    WHERE a.key = 'verifies_target'
    AND a.value::uuid IN (
        WITH RECURSIVE mod AS (
            SELECT id FROM nodes WHERE name = 'ContextRouter' AND node_type = 'namespace'
            UNION ALL
            SELECT n.id FROM nodes n JOIN mod m ON n.parent_id = m.id
        )
        SELECT id FROM mod
    )
);
```

### Q2: "What tests are failing?"

```sql
SELECT n.id, n.name,
       ts.value AS status,
       lr.value AS last_run,
       dm.value AS duration_ms
FROM nodes n
JOIN node_attributes ts ON ts.node_id = n.id AND ts.key = 'status'
LEFT JOIN node_attributes lr ON lr.node_id = n.id AND lr.key = 'last_run'
LEFT JOIN node_attributes dm ON dm.node_id = n.id AND dm.key = 'duration_ms'
WHERE n.node_type = 'test_case'
AND ts.value = 'failing'
ORDER BY lr.value DESC NULLS LAST;
```

This is a single indexed query — `idx_nodes_type` on `node_type` + the attribute joins. No graph traversal needed.

### Q3: "What tests cover this code node?"

Given a code node ID, find all test cases that verify it (directly or transitively via parent nodes).

```cypher
-- Direct coverage
MATCH (t:CodeNode)-[:VERIFIES]->(c:CodeNode {node_id: '<target_id>'})
WHERE t.node_type = 'test_case'
RETURN t.node_id, t.name

-- Transitive: tests that verify any ancestor of the target
-- (a test on a class covers all its methods)
MATCH (t:CodeNode)-[:VERIFIES]->(c:CodeNode)
WHERE c.node_id IN ['<target_id>', '<parent_id>', '<grandparent_id>']
AND t.node_type = 'test_case'
RETURN t.node_id, t.name
```

The ancestor IDs come from the relational `parent_id` chain (a simple loop or recursive CTE). The Cypher query then checks graph edges.

### Q4: "Run all P0 tests"

```sql
-- Fetch all P0 test cases with their run commands
SELECT n.id, n.name, cmd.value AS command
FROM nodes n
JOIN node_attributes pri ON pri.node_id = n.id AND pri.key = 'priority'
JOIN node_attributes cmd ON cmd.node_id = n.id AND cmd.key = 'command'
WHERE n.node_type = 'test_case'
AND pri.value = 'p0'
ORDER BY n.sibling_order;
```

An agent would:
1. Run this query to get the list
2. Execute each `command` value
3. Update `last_run`, `last_result`, and `duration_ms` attributes via `SetAttribute`
4. Update `status` to `passing` or `failing`

### Q5: "What plan specs have no test implementation?"

```sql
-- test_spec nodes that have no VERIFIED_BY edge to a test_case
SELECT n.id, n.name, n.value
FROM nodes n
WHERE n.node_type = 'test_spec'
AND n.id NOT IN (
    -- This requires a Cypher subquery or a verifies_target attribute
    SELECT a.node_id FROM node_attributes a
    WHERE a.key = 'verified_by'
);
```

Or via Cypher:

```cypher
MATCH (s:CodeNode)
WHERE s.node_type = 'test_spec'
AND NOT (s)-[:VERIFIED_BY]->(:CodeNode {node_type: 'test_case'})
RETURN s.node_id, s.name
```

## 5. Integration with Plan Contract

### Current State

Plans already have `test_spec` nodes under the CLOSE_OUT phase:

```
plan_phase "CLOSE_OUT"
├── test_spec "PlanSeeder inserts correct node count"          [unit, pending]
├── test_spec "PlanRenderer produces readable output"          [unit, pending]
├── test_spec "Round-trip: plan → DB → rendered text"          [integration, pending]
└── test_spec "AGE graph queries traverse plan-idea-code"      [integration, pending]
```

These are requirements — they say what to test, not how. They have `test_type` and `status` attributes but no `command`, `expected_output`, or execution tracking.

### Proposed Integration

`test_spec` stays as the requirement. `test_case` is the implementation. They connect via `VERIFIED_BY`:

```
plan_phase "CLOSE_OUT"
└── test_spec "PlanSeeder inserts correct node count"
        ↓ VERIFIED_BY
    test_suite "Plan Seeder Tests"
    └── test_case "T-PS-001: Seed and count nodes"
        ├── test_step [setup] "Create fresh project"
        ├── test_step [action] "Run PlanSeeder.Seed()"
        ├── test_step [assertion] "Assert node count = 27"
        └── test_step [assertion] "Assert all parent_id values are valid"
```

### Lifecycle Sync

When a `test_case` status changes to `passing`, the agent can propagate:
1. Check if all `test_case` nodes linked via `VERIFIED_BY` from a `test_spec` are `passing`
2. If yes, update the `test_spec` status to `completed`
3. If any are `failing`, update the `test_spec` status to `blocked`

This is an application-layer operation (consistent with Design Decision #1 — application-layer sync, not triggers).

### Plan-Level Test Summary

A plan's test readiness can be computed:

```sql
-- For a given plan, count test_spec nodes by verification status
WITH plan_specs AS (
    SELECT n.id
    FROM nodes n
    WHERE n.node_type = 'test_spec'
    AND n.parent_id IN (
        WITH RECURSIVE plan_tree AS (
            SELECT id FROM nodes WHERE id = @plan_id
            UNION ALL
            SELECT c.id FROM nodes c JOIN plan_tree p ON c.parent_id = p.id
        )
        SELECT id FROM plan_tree
    )
)
SELECT
    COUNT(*) AS total_specs,
    COUNT(*) FILTER (WHERE a.value = 'completed') AS verified,
    COUNT(*) FILTER (WHERE a.value = 'pending') AS unverified
FROM plan_specs ps
JOIN node_attributes a ON a.node_id = ps.id AND a.key = 'status';
```

## 6. Advantages over Files

### Why Not Just Markdown Test Plans?

Markdown test plans (e.g., `tests/test-plan.md`) are common but have fundamental limitations that the node model solves.

| Capability | Markdown Files | Node Model |
|-----------|---------------|------------|
| **Query by status** | Grep for `[FAIL]` markers — fragile, format-dependent | `WHERE status = 'failing'` — indexed, reliable |
| **Query by type** | Grep for `smoke` in text — false positives from descriptions | `WHERE test_type = 'smoke'` — exact attribute match |
| **Query by priority** | Manual parsing of header conventions | `WHERE priority = 'p0'` — first-class attribute |
| **Coverage tracking** | Manual cross-referencing between test files and code | `MATCH (t)-[:VERIFIES]->(c)` — graph traversal |
| **Execution history** | Separate CI logs, disconnected from the plan | `last_run`, `last_result`, `duration_ms` on the node |
| **Dependency ordering** | Comments like "run after T-001" — not machine-readable | `REQUIRES` edges — graph-queryable |
| **Regression provenance** | Comments like "added for bug #123" — not queryable | `GUARDS` edge to the affected node |
| **Cross-domain links** | File paths or URLs — break when things move | UUID-based edges — stable across renames |
| **Agent consumable** | Parse markdown, handle format variations | `GetSubtree()`, `GetChildren()` — typed API |
| **Semantic search** | Not possible without external indexing | `SemanticSearch()` with pgvector — built-in |
| **Concurrent updates** | File locking or merge conflicts | Advisory locks per subtree — conflict-free |

### Agent Workflow Example

An agent working on a failing build:

1. **Find failing tests**: `SELECT ... WHERE status = 'failing'` — instant
2. **Find what code they cover**: `MATCH (t)-[:VERIFIES]->(c)` — graph hop
3. **Find related ideas/plans**: `MATCH (c)<-[:PRODUCES]-(s)<-[:IMPLEMENTED_BY]-(i)` — two more hops
4. **Fix the code, re-run tests, update status**: `SetAttribute(testId, "status", "passing")`

With markdown files, steps 2-3 require the agent to parse file contents, follow naming conventions, and hope cross-references are maintained. With nodes, it's structured queries on indexed data.

### Embedding Bonus

Test case nodes get embeddings like any other node. This enables:

- **"Find tests similar to this failing test"** — pgvector similarity on the test description
- **"What tests are related to this idea?"** — semantic similarity between idea and test embeddings
- **"Suggest tests for this new code"** — find existing tests whose embeddings are close to the new code's embedding

## 7. Implementation Notes

### No Schema Changes Required

The existing `nodes` + `node_attributes` tables handle everything. New node types (`test_suite`, `test_case`, `test_step`) are just new string values in the `node_type` column. New edge labels (`VERIFIES`, `VERIFIED_BY`, `REQUIRES`, `GUARDS`) need to be added to `AgeLayer.ValidEdgeTypes`.

### NodeTypes.cs Additions

```csharp
// Test domain
public const string TestSuite = "test_suite";
public const string TestCase = "test_case";
public const string TestStep = "test_step";
```

### File ID

Test nodes use `file_id = NULL`, same as plans and conversations. They don't map to source files — they *reference* code nodes that do.

### Seeder Pattern

A `TestSuiteSeeder` would follow the same pattern as `PlanSeeder`:

```csharp
public class TestSuiteSeeder
{
    public async Task<Guid> Seed(Guid projectId)
    {
        var suiteId = Guid.NewGuid();
        await _repo.InsertNode(suiteId, projectId, null,
            "test_suite", "Smoke Tests — Context Router", "...",
            null, 0, "claude-opus-4-6",
            new() {
                { "status", "active" },
                { "run_command", "dotnet test --filter Category=Smoke" }
            });

        var caseId = Guid.NewGuid();
        await _repo.InsertNode(caseId, projectId, null,
            "test_case", "T-001: Ideation intent classification", "...",
            suiteId, 100, "claude-opus-4-6",
            new() {
                { "priority", "p0" },
                { "test_type", "smoke" },
                { "status", "pending" },
                { "command", "dotnet test --filter T001" }
            });

        // Steps under the case...
        return suiteId;
    }
}
```

### Renderer

A `TestPlanRenderer` walks the test suite subtree and emits a human-readable test plan:

```
TEST SUITE: Smoke Tests — Context Router [active]
Command: dotnet test --filter Category=Smoke

  T-001: Ideation intent classification [P0, smoke, PENDING]
    1. [setup]     Create fresh project
    2. [action]    Send '@haiku I have an idea for voice input'
    3. [assertion] Assert intent = 'ideation' (expected: ideation)
    4. [assertion] Assert context includes open threads

  T-002: Parking intent bypasses model [P0, smoke, PASSING]
    Last run: 2026-03-09T14:30:00Z (42ms)
    1. [action]    Send 'park this thread'
    2. [assertion] Assert no model call
    3. [assertion] Assert thread status = 'parked'
```

### Execution Tracking

After running a test, the agent updates attributes:

```csharp
await repo.SetAttribute(caseId, "last_run", DateTime.UtcNow.ToString("o"));
await repo.SetAttribute(caseId, "last_result", "pass");
await repo.SetAttribute(caseId, "duration_ms", "42");
await repo.SetAttribute(caseId, "status", "passing");
```

No new repository methods needed — `SetAttribute` with its `ON CONFLICT ... DO UPDATE` handles both insert and update.

## 8. Open Questions

1. **Should `test_suite` nest?** Suites containing suites (e.g., "All Tests" → "Smoke" → "Regression") adds complexity. Proposal: allow nesting via `parent_id` — the recursive CTE already handles arbitrary depth. Start flat, nest when needed.

2. **Flaky test detection**: The `fail_count` attribute tracks consecutive failures. Should there be a `flaky` status distinct from `failing`? Proposal: yes — `flaky` means "sometimes fails" which is a different signal than "always fails." Threshold: 3+ alternating pass/fail in the last 10 runs.

3. **Test run history**: Current design tracks only the last run. Should there be `test_run` nodes (one per execution) as children of `test_case`? Proposal: defer — single-attribute tracking is enough for the POC. Run history can be added later as child nodes without changing the existing schema.

4. **Parameterized tests**: A test case run with multiple inputs (e.g., "classify intent for each of 20 sample messages"). Proposal: each parameterized variant is a separate `test_case` node, with a `parameter_set` attribute linking them. The suite's `test_case` children with the same `parameter_set` value form a group.
