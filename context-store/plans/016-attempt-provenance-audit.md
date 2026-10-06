# Plan 016 — Attempt provenance audit and executor reference (increment 3a)

**Status:**
- Approved by codex-hekate (msg 505), with the corrections folded in below.
- **Accepted by codex-hekate after independent source review and checks.**
- Evidence: [017](017-attempt-provenance-validation.md); PlanContracts.Tests 146/146, PlanStore.LiveTests 34/34, `scripts/local/PlanContractApi.Tests.ps1` 39/39.

**Scope (narrowed by codex-hekate, msg 499):**
- In scope: an attempt-provenance audit trail, plus an optional opaque executor reference on attempts.
- Not in scope:
  - `claim-next` (a durable claim receipt needs its own design)
  - launching workers
  - mirroring status from or to any execution ledger
  - changes to the gods pipeline
  - backfilling history

It builds on [012](012-plan-node-contracts-v1.md) (2a/2b1) and [015](015-execution-ledger-inventory.md).

## 1. Principle

- `plan_node_state` stays the only authority for plan work state.
- The audit trail is a **derived, append-only record of applied contract operations**. It is written in the same transaction and never read by PlanRules or used to decide anything. It is not a second ledger.
- The executor reference is an opaque pointer **from** a plan attempt **to** an executor's private run record. The plan store never reads or writes the executor's ledger.

## 2. Pure rules changes (compatible)

- **`executorRef` is caller-provided, opaque correlation.** It is never treated as verified agent identity or claim authority, and never consulted for authorization. A review's actor and evidence are independent of it.
- `NodeState` gains `ExecutorRef` (`string?`). It is appended as the **last** positional member with a default of `null`, and `NodeState.Initial` keeps `null`. Existing callers keep compiling: tests use `NodeState.Initial with {…}`, and PlanStore is updated.
- `PlanRules.Transition(..., string? executorRef = null)`: a new optional last parameter.
  - **Start or reopen** (`→ InProgress`): sets `ExecutorRef = executorRef`. A reopen binds the new reference and never inherits the old one.
  - **Finish** (`InProgress → Done`): keeps the current `ExecutorRef`, which records which run produced the artifact. An `executorRef` argument on finish must be `null` or equal to the current one; otherwise `stale_attempt`.
  - **Release** (`InProgress → Todo`) and **cancel**: clear `ExecutorRef` together with `AttemptId`. The audit event records both values *before* clearing (section 4).
  - **Restore** (`Cancelled → Todo`): stays `null`.
  - `executorRef` is part of the transition fingerprint, length-prefixed.
- Validation (`ValidateState`, giving `invalid_state`):
  - `ExecutorRef` must be `null` whenever `AttemptId` is `null`.
  - When set: 1 to 256 characters, printable ASCII `0x21`–`0x7E` only (no spaces or control characters).
  - Containers must have `null`.
- A new error code is likely unnecessary: a malformed reference on input gives `invalid_executor_ref` (400), checked before Prologue like `invalid_enum`. The coverage test is extended accordingly.

## 3. Schema (additive; created under the existing 2b1 opt-in)

- `plan_node_state.executor_ref text NULL`: `ADD COLUMN IF NOT EXISTS`, no default backfill.
- `managed_plans.event_seq bigint NOT NULL DEFAULT 0`: the per-plan event counter, advanced only by PlanStore under the project lock.
- **`plan_attempt_events`** (append-only):

| column | meaning |
|---|---|
| `root_node_id` + `seq` | **primary key**. Deterministic identity and ordering within a plan (`seq` = `managed_plans.event_seq + 1…n`, under the project lock) |
| `node_id`, `node_state_revision` | **unique**. The state revision the operation produced; each applied operation bumps the node's state revision exactly once |
| `kind` | `attempt_started`, `attempt_reopened`, `attempt_finished`, `attempt_released`, `attempt_cancelled`, `work_restored`, `decision_recorded`, `content_revised` |
| `work_from`, `work_to` | work status before and after |
| `content_revision` | the node's content revision **after** the operation |
| `attempt_id`, `attempt_epoch`, `executor_ref` | the attempt **as it was during the operation**: for release and cancel, the values *before* clearing |
| `artifact_ref` | the artifact after finishing; for a decision, the reviewed artifact |
| `decision`, `reviewed_content_revision`, `evidence_ref` | for `decision_recorded` |
| `content_digest` | for `content_revised`: the payload digest from `PlanContentDigest` |
| `actor`, `operation_key` | from the operation context |
| `recorded_at` | `DEFAULT now()`. The database clock is used only for display and never in rules or ordering |

- Dependency edits and `AddChild` produce **no** events in 3a: this is an attempt audit, not a full change log. A plan-level structural log could follow later.

## 4. Event derivation (pure, testable)

`PlanAuditEvents.Derive(PlanGraph before, PlanGraph after, Guid nodeId, OpKind kind, OperationContext ctx, …)` returns 0 or 1 events. It lives in `PlanContracts/` and is pure:

| Applied operation | Event |
|---|---|
| Transition Todo→InProgress | `attempt_started` (new epoch, ref) |
| Transition Done→InProgress | `attempt_reopened` (new epoch, new ref) |
| Transition InProgress→Done | `attempt_finished` (epoch, ref, artifact) |
| Transition InProgress→Todo | `attempt_released` (the **previous** attempt id, epoch and ref) |
| Transition *→Cancelled | `attempt_cancelled` (previous attempt id/epoch/ref, may be null; `work_from` records where it came from) |
| Transition Cancelled→Todo | `work_restored` |
| Decide (Applied) | `decision_recorded` |
| ReviseContent (Applied) | `content_revised` (new content revision, digest) |
| AddDependency, RemoveDependency, AddChild, CreatePlan | none |
| any **Unchanged** (replay or identical decision) or **Rejected** | **none** |

## 5. Store semantics

- In `MutateAsync`, after the PlanRules operation returns **Applied** and before projection, derive the events. In the same transaction:
  1. `UPDATE managed_plans SET event_seq = event_seq + n RETURNING event_seq`
  2. `INSERT` the events with consecutive `seq` values
- If `event_seq + n` would overflow a `bigint`, the operation is rejected with `revision_exhausted` (HTTP 409) **before** any write.
- Rollback for any reason (compare-and-set, projection, constraint, overflow) leaves neither events nor a `seq` advance.
- **Gap-free and contiguous holds for store-generated transactions only.** A holder of full database credentials can insert events or advance the sequence directly, the same limit as the 2b1 fence.
- PlanStore has **only** an insert method for events. There are no update or delete methods.

## 6. Append-only protection (stronger than the 2b1 fence)

- Trigger `BEFORE UPDATE OR DELETE` on `plan_attempt_events`: **always** raises HP409 (`managed_plan_protected: plan_attempt_events is append-only`), **even when `hekate.plan_contract='on'`**.
- `BEFORE TRUNCATE`: always raises.
- `BEFORE INSERT`: requires `hekate.plan_contract='on'`, as the other contract tables do.
- `managed_plans.event_seq` may only advance: a trigger on `managed_plans` rejects any update that decreases `event_seq`.
- Honest limit, as in 2b1: a database superuser can disable triggers. This is integrity, not tamper-proofing.

## 7. API (same opt-in, loopback, error mapping)

- `POST nodes/{id}/transition` accepts an optional `executorRef`.
- `GET plans/{root}/events?afterSeq=<n ≥ 0>&limit=<1..500, default 100>`: ascending `seq`. Returns `{ events:[…], nextAfterSeq, historyStartsAtSeq, historyBackfilled:false }`.
- `GET nodes/{id}/events?afterSeq=&limit=`: same, filtered by node and still ordered by plan `seq`; `afterSeq` is the plan `seq`.
- **Pagination** uses a stable `(root, seq)` cursor:
  - The query fetches `limit + 1` rows with `seq > afterSeq`, after applying the node filter.
  - `nextAfterSeq` is the last returned `seq` when more rows exist, otherwise null.
  - Events appended between calls are fine: they come after the cursor.
- **Errors:** 404 `plan_not_found` / `node_not_found` for a missing plan or node; 400 `invalid_query` for a non-numeric or negative `afterSeq`, or a `limit` outside 1..500.
- The node view adds `executorRef`.
- Event JSON uses snake_case enum names, plus `contractVersion`.

## 8. No backfill

Plans created before 3a have attempts with no history, and nothing is synthesised. The first event appears with the next applied operation.

`historyStartsAtSeq` is only the first **recorded** event's `seq` (or null). It is **not** proof of the earliest work. `historyBackfilled` is always `false`. For a node whose attempt epoch is above 0 when 3a is installed, its earlier history is unknown.

## 9. Files

- **New:**
  - `PlanContracts/PlanAuditEvents.cs` (pure derivation)
  - `PlanContracts.Tests/AuditEventTests.cs`
  - additions to `PlanStore.LiveTests` and `scripts/local/PlanContractApi.Tests.ps1`
- **Modified:**
  - `PlanModel.cs`: `NodeState.ExecutorRef`, the `AuditEvent` record, `invalid_executor_ref`
  - `PlanRules.cs`: Transition parameter, validation, fingerprint
  - `PlanStoreSchema.cs`: column, sequence, table, triggers
  - `PlanStore.cs`: event writes, load and persist of `executor_ref`, event queries
  - `Api/PlanContractEndpoints.cs`: `executorRef` and the event endpoints
  - 012 (§3a pointer)
- **Not touched:** Odin and gods, legacy writers, `Program.cs` (no new wiring needed), the launcher.

## 10. Tests

**Pure:**
- Every row of the derivation table.
- Unchanged and Rejected produce no event.
- Release and cancel capture the *previous* attempt and reference.
- A reopen binds the new reference.
- A finish with a different reference gives `stale_attempt`.
- The reference is in the fingerprint: same key, different reference gives `operation_key_reused`.
- Validation of malformed and orphan references.
- Existing 2a/2b1 suites stay green.

**Live:**
- start → finish → decide → reopen(new ref) → release → start, giving events `seq` 1…n in order with the exact fields.
- Replay and rejected operations write no event and leave `event_seq` unchanged.
- Concurrent operations on two nodes of one plan give contiguous, unique `seq` values.
- An AGE projection failure leaves no event and no `seq` advance.
- With the contract flag `'on'`, a raw UPDATE or DELETE on events, a TRUNCATE, or decreasing `event_seq` all give HP409.
- Pagination: limit, `afterSeq`, `nextAfterSeq`.
- An existing plan with no history reports `historyStartsAtSeq`.

**HTTP script (read-only events):**
- executor reference round-trip;
- events endpoint ordering and pagination;
- 404 for a missing plan or node, 400 for a bad `afterSeq` or `limit`.

There is no public history-write endpoint, so append-only protection is covered by the live SQL HP409 tests only. No write API is added just for tests.

## 11. Explicit limits (unchanged by 3a)

- No claim protocol (`claim-next` and durable claim receipts need a separate design that pins plan and content revisions).
- No auth: actor, attempt id and executor reference are opaque correlation values.
- No link validation: the plan store does not check that `executorRef` exists in any executor.
- No executor integration, and no pinning of an executor to a content revision beyond what `decide` already enforces.
- No full structural change log.
- Superuser bypass is possible.
