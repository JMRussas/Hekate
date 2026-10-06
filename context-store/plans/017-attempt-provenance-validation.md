# Plan 017 — Attempt provenance audit (increment 3a): validation evidence

**Status: accepted by codex-hekate after independent source review, clean-source tests, HTTP checks and a real schema upgrade. Scope: increment 3a only.**

**Date:** 2026-10-06
**Host:** fenrir (Windows 11, Docker Desktop 29.8.1, PostgreSQL 16 + AGE 1.6.0 + pgvector 0.8.0 in the owned `hekate-local` container)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate

Design and exact semantics: [016](016-attempt-provenance-audit.md). Ledger evidence behind the scope: [015](015-execution-ledger-inventory.md).

## Results

| Suite | Command | Result |
|---|---|---|
| Pure rules | `dotnet test context-store/PlanContracts.Tests` | **146 / 146** |
| Live store (PostgreSQL + AGE) | `HEKATE_PLAN_LIVE_CONNSTR=… dotnet test context-store/PlanStore.LiveTests` | **34 / 34** |
| Api process (HTTP) | `pwsh scripts/local/PlanContractApi.Tests.ps1` | **39 / 39**, exit 0, database verified dropped |
| Api build | `dotnet build context-store/Api/Api.csproj` | 0 errors |

codex-hekate's independent runs: **146 pure tests and 34 live tests** passed on a clean source overlay that excludes unrelated uncommitted changes. **39 HTTP checks** passed against a new disposable database. The final test-cleanup adjustment was also verified by rerunning the focused real-upgrade test.

**Isolation:**
- Every live run used new `hekate_plan_live_*` / `hekate_plan_api_*` databases, and dropped them.
- The live fixture drops only a database it created itself (`_created` flag set after `CREATE DATABASE` succeeds).
- No pre-existing database was modified or downgraded.
- No production host, NSSM service, model, Odin/gods code or legacy writer was touched.

## Acceptance criteria and evidence

| Criterion (016 and review messages 505, 511, 512, 517, 522) | Evidence |
|---|---|
| Executor reference: start/reopen binds, finish keeps, release/cancel clear, reopen never inherits, a mismatching finish is a stale attempt | Pure `ExecutorRefTests`; live `Full_attempt_history…`; HTTP `C view keeps executorRef after finish` |
| Reference validated (1–256 printable ASCII, attempt required, containers null) and part of the fingerprint | Pure `Malformed_references…`, `Stored_references_are_validated`, `Reference_is_part_of_the_fingerprint`; HTTP `E invalid executorRef 400` |
| Original 2b1 fingerprint unchanged when the reference is null (upgrade-compatible replay) | Pure `A_2b1_stored_fingerprint_still_replays…`, with the frozen literal `10:transition;6:tester;10:InProgress;2:x1;~;~;` |
| Event derivation table; release and cancel capture the attempt before clearing; no event for Unchanged, Rejected or structural operations | Pure `AuditEventDerivationTests`; live `Full_attempt_history…` (9 events, exact fields) and `Replays_rejections_and_structural_ops_write_no_events` |
| Events written in the same transaction; contiguous per-plan `seq`; unique `(node, state revision)` | Live `Full_attempt_history…` (`seq` 1..9), `Concurrent_operations_on_two_nodes…` (`seq` [1, 2]) |
| Rollback leaves no trace (AGE failure, sequence overflow gives `revision_exhausted`) | Live `AGE_failure_and_seq_exhaustion_leave_no_trace`: state, revision, executor reference, events and tagged edges unchanged |
| Append-only even with the store flag set | Live `Events_are_append_only_even_for_the_store`: UPDATE, DELETE, TRUNCATE and a decreasing `event_seq` are rejected with HP409; INSERT without the flag is rejected with HP409 |
| Pagination: stable `(root, seq)` cursor, `limit+1`, node filter, appends between calls | Live `Pagination_is_a_stable_seq_cursor_with_node_filter`; HTTP `E pagination cursor`, `E node events filtered` |
| 404 for a missing plan or node versus an empty history; argument guards; 400 for an invalid query | Live `Missing_plan_or_node_is_not_found_but_empty_history_is_a_page`; HTTP `E missing plan/node 404`, `E existing node with no history is 200 empty`, `E invalid query 400` (5 variants) |
| `historyStartsAtSeq` is the first recorded event only; `historyBackfilled=false`; no synthesis | HTTP `E history markers`; live upgrade tests (`historyStartsAtSeq` null, 0 events) |
| **Real schema upgrade from the 2b1 shape** | Live `Real_schema_upgrade_from_the_2b1_shape_adds_3a_and_preserves_existing_state`, detailed below |

### Real schema-upgrade test (msg 522)

The test runs on a **separate, fresh, test-owned** database. The shared fixture is never downgraded.

1. Remove only the 3a additions: the events table and its 3 guard triggers, the `event_seq` guard trigger and both functions, `plan_node_state.executor_ref`, and `managed_plans.event_seq`. Assert through `information_schema` that the columns and table are absent.
2. Insert 2b1-era rows with the store flag set: a plan root and an in-progress attempt (`x1`, epoch 1) carrying the stored 2b1 start key and fingerprint.
3. Run `PlanStoreSchema.Ensure` twice. Then assert:
   - both columns and the events table exist, and there are 14 guard triggers;
   - the task's state row is **byte-identical** as canonical `jsonb`, except for the new `executor_ref`, which is NULL;
   - `event_seq` is 0 and no events exist.
4. The 2b1 retry, with the same key and payload and no reference, returns **Unchanged**. Still 0 events. The reloaded state is `in_progress`, `x1`, epoch 1, state revision 1.

## Defects found by review and fixed before acceptance

| Defect | Fix |
|---|---|
| The `AppendEvents` comment claimed the overflow check ran "before any write" | Comment corrected: the check runs after the state writes and the whole transaction rolls back |
| The public `ReadEventsAsync` accepted unscoped or out-of-range arguments | Exactly one selector is required; `afterSeq ≥ 0`; `1 ≤ limit ≤ 500` (tested) |
| The fingerprint compatibility test mirrored the encoder | Frozen 2b1 literals in both suites |
| The upgrade test simulated old rows inside the new schema | A real schema upgrade on a separate fresh database |
| The live fixture could drop a database it did not create | `_created` flag |

## Known limits (unchanged by 3a)

- No claim protocol (`claim-next` or durable claim receipts).
- No auth: the actor, attempt id and executor reference are opaque correlation values.
- The executor reference is not checked against any executor ledger.
- No structural change log.
- Gap-free sequencing holds for store-written transactions only; a holder of full database credentials can bypass the triggers.
- Plans created before 3a have no attempt history, and none is backfilled.
