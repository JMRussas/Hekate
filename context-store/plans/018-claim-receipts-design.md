# Plan 018 — Durable claim receipts (design only)

**Status: proposal, reviewed by codex-hekate (msg 533). Partly SUPERSEDED by the approved and implemented [019](019-durable-claims-and-pins.md) (increment 3b1): durable receipts, content and prerequisite pins, and strict invalidation (sections 2, 3, 5a–5c) are settled there, and 019 wins wherever the two differ. Leases, heartbeats, expiry and reclaim (section 4) remain deferred to 3b2 and are NOT approved; no code without a new GO.**
**Builds on:** [012](012-plan-node-contracts-v1.md) (rules and store), [016](016-attempt-provenance-audit.md) / [017](017-attempt-provenance-validation.md) (attempt audit, executor reference), [015](015-execution-ledger-inventory.md) (ledger facts).

## 0. Why the earlier `claim-next` sketch was unsafe

Replaying a claim by looking up the node's *latest* operation key breaks in two ways:

- **A later mutation erases the key.** If the node is finished, released or otherwise changed after the claim, the key is no longer the node's latest. A retry of the original claim then looks new, and claims a different node.
- **Nothing pins the content.** The worker could execute requirements that changed after the claim.

A claim needs its own durable, immutable record.

## 1. Concepts

| Term | Meaning |
|---|---|
| **Claim request** | "Give me the next ready leaf of plan *R* to work on", identified by `(R, claimKey)` and bound to its exact payload |
| **Claim receipt** | The **immutable historical** answer to one claim request, recorded once and returned identically on every retry. It describes what was granted *at the time*. It is **not** current authority |
| **Current authority** | Always `plan_node_state` (work, attempt id and epoch, lease). A receipt never overrides it |
| **Lease** | A time bound on an attempt's exclusivity, kept by the store with the database clock. An expired lease does not release anything by itself |

**A receipt is not permission to (re-)execute external effects.** Commits, pushes, file writes and commands belong to the executor. Holding or replaying a receipt says only what was granted.

**`stillCurrent`** is true only when **all** of these hold at read time:

- the node is InProgress;
- its attempt id **and** epoch equal the receipt's;
- the node's current content revision and digest equal the receipt's pin;
- the prerequisite snapshot still matches (section 5b);
- the lease has not expired.

For a `no_ready_work` receipt, `stillCurrent` is false and `current` is null.

`stillCurrent` is a **factual correlation**, never proof that "this worker owns the attempt", and never authenticated authority.

**`stillCurrent` is not sufficient protection for external effects.** Checking it and then acting is a time-of-check/time-of-use race: the attempt can be reclaimed or cancelled between the check and a git push or command. The store cannot fence effects outside the database. Safe execution additionally needs, in the executor:

- isolated workspaces;
- a single integration owner;
- idempotent effects;
- reconciliation before any retry.

None of that exists yet, so nothing is activated.

## 2. Claim operation

`POST /api/plan-contract/v1/plans/{root}/claims`

```
{ claimKey, attemptId, executorRef?, actor, leaseSeconds (60..86400, default 3600) }
```

All fields are required except `executorRef` and `leaseSeconds`. The response is `{ receipt, stillCurrent, current: {work, attemptId, attemptEpoch, leaseExpiresAt} }`.

**Store transaction** (same pattern as 2b1/3a: fence, then per-project lock, then snapshot):

1. Look up a receipt at `(root, claimKey)`.
   - **Found, same payload fingerprint:** return it unchanged, with `stillCurrent` computed from current state. **Do not claim anything.**
   - **Found, different payload:** 409 `operation_key_reused`.
2. Not found: run `PlanRules.Evaluate` and take the **first ready leaf in hierarchy order**. The choice is deterministic and made on the server, under the lock.
   - **None ready:** record a receipt with `outcome = no_ready_work`. A retry with the same key keeps answering "no work", even if work becomes ready later; the caller uses a new key to ask again.
   - **Leaf found:** apply `Transition(start)` through the pure rules, with the `attemptId`, the `executorRef` and the operation key `claim:<claimKey>`. Pin the attempt to the node's content (section 3), set the lease, write the 3a `attempt_started` event (now carrying `claim_key`), and **insert the receipt**, all in one transaction.
3. Commit. Any failure rolls back the receipt, the state, the event and the projection together.

**Payload fingerprint:** length-prefixed `{actor, attemptId, executorRef, leaseSeconds}`.

## 3. Exact content pinning

- `plan_node_state` gains `attempt_content_revision` (set at start, cleared with the attempt).
- **Pinning applies to every start path,** not only claims. The generic `Transition` to InProgress, including reopen, sets the pin in the pure rules, so no path starts unpinned work.
- **Attempts started before pinning existed** (`attempt_content_revision` NULL while InProgress) **fail closed on finish** with `stale_content`. They must be released, or explicitly migrated by a separately approved operation, before they can finish. Nothing is backfilled.
- The receipt stores `node_content_revision`, `content_digest` (`PlanContentDigest` of the value and content attributes), and a **content snapshot**: the value and content attributes as JSON, immutable. The worker executes **exactly that snapshot**.
- **Finish is rejected with `stale_content`** when the node's current content revision differs from `attempt_content_revision`. The executor must release, and a new claim picks up the new specification. `Decide` keeps its existing exact-revision rule.
- Revising content while work is in progress stays allowed, as a planner's prerogative. It simply makes that attempt unable to finish.

## 4. Release, cancel and expiry

| Event | Who | Effect |
|---|---|---|
| Release | The owner (presents attempt id and epoch) | Back to Todo, attempt and lease cleared; `attempt_released` event |
| Cancel | Planner | Cancelled, attempt and lease cleared; a late finish is fenced by the epoch |
| Heartbeat | The owner (`POST nodes/{id}/heartbeat {attemptId, attemptEpoch, actor, leaseSeconds}`) | Extends `lease_expires_at` using the database clock. Changes **no** state revision and writes no audit event (only `last_heartbeat_at`). Rejected if the attempt is not current. **Rejected once the lease has expired:** heartbeats never resurrect an expired attempt. The new expiry is `max(existing, now + leaseSeconds)`, so a heartbeat **never shortens** a lease |
| **Expiry** | Nobody, automatically | **Nothing changes.** The node stays InProgress with its attempt. An expired lease only makes the node *reclaimable* |
| Reclaim | Planner or operator (`POST nodes/{id}/reclaim {expectedStateRevision, operationKey, actor, reconciliationRef}`) | Allowed only when the lease has expired. Requires a non-empty `reconciliationRef`, evidence that the previous attempt's external effects were inspected. Moves InProgress to Todo, with an `attempt_reclaimed` event recording the old attempt, executor reference and reconciliation reference. A **new** claim then issues epoch + 1, which fences the old worker. **No automatic re-execution** |

The lease is store state, kept outside the pure rules because it depends on the database clock. The pure rules treat the operation as a release with a different event kind.

- **Finish after expiry is rejected** (`lease_expired`, HTTP 409), even when the attempt id and epoch still match. Expired work cannot be committed into the plan; the node must be reclaimed and claimed again.
- **Claim, heartbeat, finish, release, cancel and reclaim all serialise under the per-project lock.** Each samples the database clock **once, after acquiring the lock**, with `clock_timestamp()` (not `now()` / `transaction_timestamp()`, which is fixed at transaction start and can predate a long lock wait; codex-hekate msg 542), and uses that single value for every expiry decision in the operation.

## 5. Exclusive ownership, durability and crash reconciliation

- **Exclusivity:** one active attempt per node is guaranteed by the per-project lock, compare-and-set, and the epoch fence. Two concurrent claims serialise under the lock; the second one sees the node in progress and picks the next ready leaf, or records `no_ready_work`.
- **Durability:** the receipt, state, event and projection commit atomically. Nothing exists half-claimed.
- **Client crash after commit, before the response:** retry with the same `claimKey` and get the original receipt. `stillCurrent=true` only means the stored facts still match the receipt (factual correlation, section 1); it is not ownership, authority, or permission to perform external effects.
- **Worker crash mid-execution:** the lease expires, then an operator reconciles the external effects and reclaims. The next claim's new epoch fences any late result from the crashed worker.
- **Store crash mid-transaction:** PostgreSQL rolls back; no receipt is recorded, so a retry claims afresh.

## 5a. Unresolved: pinning prerequisites, not just node content

A claim was granted because the node's gates held at that moment. Gates depend on dependencies declared on the leaf **and on its ancestors**: each predecessor's work, effective acceptance, content and artifact. Pinning only the node's own content misses upstream changes.

## 5b. Proposed prerequisite snapshot (no new "plan revision")

There is no plan-wide revision, and none should be invented. Instead the receipt records a **prerequisite vector**. It has one entry per gating predecessor, collected over the leaf and all its ancestors' dependencies:

```
{ predecessorId, gate, contentRevision, stateRevision, attemptEpoch, artifactRef, effectiveAcceptance }
```

Entries are sorted by `(owner, predecessorId)`, and the vector is reduced to a canonical `prereq_digest` using the same length-prefixed SHA-256 scheme as `PlanContentDigest`. Artifact references here are the predecessors' artifacts: the prerequisite artifacts the worker may consume.

## 5c. Unresolved: invalidation semantics (codex-hekate to choose)

- **(a) Strict:** finish is rejected with `stale_prerequisites` when the current vector digest differs from the pinned one. The worker must release and claim again. This is the safest option, but any harmless upstream state bump (for example a predecessor re-accepted at the same content) invalidates the work.
- **(b) Gate-based:** finish is allowed, and the existing rule already blocks acceptance while gates don't hold (`gates_not_satisfied`). The pinned vector is surfaced in `stillCurrent` and the readiness view, so reviewers can see the drift.
- **(c) Hybrid:** finish is rejected only when a pinned predecessor's content revision, artifact or attempt epoch changed (the inputs the worker actually consumed). State-only and acceptance-only changes fall back to (b).

Recommendation: (c). The decision is open.

## 6. Schema (additive, under the existing opt-in)

- `plan_node_state`: `attempt_content_revision bigint NULL`, `attempt_prereq_digest text NULL`, `lease_expires_at timestamptz NULL`, `last_heartbeat_at timestamptz NULL`.
- The receipt also stores `prereq_vector jsonb` and `prereq_digest` (section 5b).
- `plan_claim_receipts`:
  - Primary key `(root_node_id, claim_key)`.
  - Columns: `request_fingerprint`, `outcome` (`claimed` | `no_ready_work`), `node_id`, `attempt_id`, `attempt_epoch`, `executor_ref`, `node_content_revision`, `content_digest`, `content_snapshot jsonb`, `lease_seconds`, `event_seq`, `actor`, `created_at`.
  - Append-only, with the same triggers as the 3a events: UPDATE, DELETE and TRUNCATE are always rejected with HP409, and INSERT requires the store flag.
- `plan_attempt_events`: add a `claim_key` column and the kind `attempt_reclaimed`. The events table is extended in place, as an additive change.
- `GET plans/{root}/claims/{claimKey}` reads a receipt.

## 7. Tests (sketch)

**Pure:**
- Claim selection is deterministic.
- `stale_content` on finish after a content revision.
- Reclaim is a release with a different event kind, and is rejected while the lease is valid (the store supplies an expired flag).

**Live:**
- Two concurrent claims get different nodes, or exactly one records `no_ready_work`.
- Replay after **later mutations** (finish, release, a new claim on another node) still returns the original receipt and claims nothing.
- A different payload under the same key is rejected with `operation_key_reused`.
- A `no_ready_work` receipt is stable after work becomes ready.
- The content snapshot equals the stored content at claim time; a revise followed by a finish gives `stale_content`.
- Heartbeat extends the lease without a state revision; a heartbeat after reclaim is rejected.
- Reclaim before expiry is refused; after expiry it requires `reconciliationRef`; the old epoch's finish is fenced.
- Receipts are append-only even with the store flag set.
- An AGE failure or a sequence overflow leaves no receipt.

**HTTP:** the claim/replay round trip, heartbeat, reclaim errors, and receipt reads.

## 7a. Additional tests implied by the review

- `stillCurrent` is false for each failing condition separately: not InProgress, a different attempt or epoch, a content change, prerequisite drift, an expired lease. It is false for `no_ready_work`.
- A heartbeat after expiry is rejected. A heartbeat never shortens the lease. A finish after expiry gives `lease_expired`.
- Concurrent heartbeat and reclaim, and concurrent finish and reclaim, serialise: exactly one outcome.
- A generic `Transition` start pins content, with no unpinned path. A legacy unpinned in-progress attempt is fail-closed on finish.
- The prerequisite vector is deterministic, and the chosen invalidation option (5c) behaves as specified.

## 8. Explicit unresolved preconditions before any worker launch

- **Auth:** `actor`, `attemptId` and `executorRef` remain opaque, caller-supplied correlation values. Nothing binds a claim to an authenticated principal. Workers must not be launched against this API until an auth boundary is chosen; for now the API is loopback-only.
- **Executor adapter:** not chosen. The gods engine has no compare-and-set on `engine_tasks`, its deployed database is unknown, and its API is unauthenticated ([015](015-execution-ledger-inventory.md)). An adapter would call claim, heartbeat, finish and release, and must never write plan state any other way.
- **Executor safety requirements** (section 1): isolated workspaces, a single integration owner, idempotent effects, and reconciliation before retry. All are unbuilt. `stillCurrent` cannot substitute for them.
- **Prerequisite invalidation choice** (5c) is open.
- **External-effect reconciliation:** the `reconciliationRef` content is policy. What counts as reconciled (a git state check, artifact verification) is undecided.
- **Clock:** leases use the database clock only. Skew between workers is irrelevant, but a database clock jump affects expiry.
- **Legacy writers** stay fenced and unmigrated.
