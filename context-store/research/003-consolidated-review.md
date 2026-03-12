# Research 003 — Consolidated Code Review (2026-03-08)

Combined review from Claude Opus 4.6 and Gemini CLI. This supersedes and extends `002-architecture-review.md` with deeper analysis and new findings.

## Reviewers

- **Claude Opus 4.6** — full file read of all source files, categorized findings
- **Gemini CLI** — independent review via `gemini -p`, focused on architecture and correctness

## New Findings (not in 002)

### N1. Advisory Lock Session Mismatch (BUG — CRITICAL)
**Where:** `AgentCoordination/SubtreeLock.cs` + `DbLayer/NodeRepository.cs`
**Problem:** `pg_try_advisory_lock` is session-scoped. SubtreeLock acquires the lock on its own connection, but NodeRepository mutations open a **new** connection. The lock doesn't protect the mutation at all — another agent could mutate the same subtree concurrently.
**Fix:** Pass the lock's connection into repository methods, or have SubtreeLock expose a method that performs mutations on the locked connection.
**Found by:** Gemini (Claude flagged the ordering issue but not the session mismatch)

### N2. Lock Release Before Validation (BUG — HIGH)
**Where:** `Program.cs:478-479`
**Problem:** `ReleaseSubtree` is called before code is regenerated and validated. Between release and rebuild, another agent could claim and mutate the subtree.
**Correct flow:** Claim → Mutate → Regenerate → Validate → Release
**Fix:** Move `ReleaseSubtree` call to after validation succeeds.
**Found by:** Claude

### N3. Cypher Injection in AgeLayer (SECURITY — HIGH)
**Where:** `GraphLayer/AgeLayer.cs:95-111, 137, 149`
**Problem:** Edge types and node names are string-concatenated into Cypher queries. `safeName` only replaces single quotes — insufficient for Cypher. Edge type is completely unsanitized.
**Fix:** Whitelist valid edge types. Use AGE parameterized Cypher or proper escaping.
**Found by:** Both

### N4. Hardcoded Credentials (SECURITY — HIGH)
**Where:** `Program.cs:28`
**Problem:** Connection string with `postgres:postgres` hardcoded in source.
**Fix:** `Environment.GetEnvironmentVariable("CODESTORAGE_CONNSTR")` with POC fallback.
**Found by:** Claude

### N5. No Transaction Boundaries (DESIGN — MEDIUM)
**Where:** `Program.cs` mutation demo, `NodeRepository.InsertNode`
**Problem:** Multi-step mutations (insert parameter + update statement) aren't wrapped in transactions. Partial failure leaves inconsistent state. InsertNode itself inserts node then attributes sequentially without a transaction.
**Fix:** Accept optional `NpgsqlTransaction` in repository methods. Wrap multi-step mutations.
**Found by:** Both

### N6. Sequential Context Assembly (PERFORMANCE — MEDIUM)
**Where:** `ContextRouter/ContextAssembler.cs:53-118`
**Problem:** `Assemble()` runs `GetConversationStats` then intent-specific queries sequentially.
**Fix:** Use `Task.WhenAll` to parallelize independent queries.
**Found by:** Gemini

### N7. AGE LOAD/SET Per Call (PERFORMANCE — LOW)
**Where:** `GraphLayer/AgeLayer.cs:33-58`
**Problem:** `LOAD 'age'` and `SET search_path` executed on every Cypher call.
**Fix:** Configure at database/user level, or use persistent connections.
**Found by:** Gemini

## Previously Known (from 002, confirmed by both reviewers)

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 002-1 | N+1 attribute subqueries in ContextAssembler.Assemble() | Medium | Partially fixed (AssembleForAgent uses JOIN) |
| 002-2 | Intent classifier brittleness | Low/Medium | Unchanged |
| 002-3 | No STT confidence scores | Low | Unchanged |
| 002-4 | Graph/relational atomicity gap | High | Unchanged |
| 002-5 | No node versioning | High | Unchanged |
| 002-6 | Embedding refresh | Medium | Unchanged |
| 002-7 | AGE sync scalability (N+1 MERGE) | Medium | Confirmed by both |
| 002-8 | Sibling order contention | Low | Unchanged |

## Additional Suggestions

- **Hardcoded node type strings** — centralize into constants class (both reviewers)
- **No NpgsqlDataSource** — Npgsql 8+ supports shared data source (both reviewers)
- **Console.WriteLine logging** — replace with structured logging for production (Claude)
- **IndentedTextWriter** for CSharpGenerator instead of manual spacing (Gemini)
- **Model selection aliases** — map to actual model IDs via config (Gemini)
- **Invariant checks** — parentId != id, siblingOrder >= 0 (Claude)

## Priority Fix Order

1. **N1 — Advisory lock session mismatch** (CRITICAL — locks are broken)
2. **N3 — Cypher injection** (HIGH — security)
3. **N2 — Lock release ordering** (HIGH — correctness)
4. **N4 — Hardcoded credentials** (HIGH — security)
5. **N5 — Transaction boundaries** (MEDIUM — data integrity)
6. **N6 — Sequential context assembly** (MEDIUM — latency)
7. **002-4 — Graph sync atomicity** (HIGH — design debt, bigger effort)
8. **002-5 — Node versioning** (HIGH — design debt, bigger effort)
