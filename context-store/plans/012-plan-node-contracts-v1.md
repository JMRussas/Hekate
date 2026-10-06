# Plan 012 — Plan node contracts v1 (`plan-contract/v1`)

**Status:**
- **Increment 2a:** pure rules, implemented.
- **Increment 2b1:** PostgreSQL store plus an opt-in loopback API, implemented and live-tested on a disposable database.
- **Increment 3a:** append-only attempt and review provenance, with an optional opaque executor reference; accepted. See [016](016-attempt-provenance-audit.md) and [017](017-attempt-provenance-validation.md).

Not wired to execution, UI, auth, or legacy-plan enrollment. See *Increment 2b1* below.
**Goal: one engine-neutral definition of how plan nodes, dependencies, work state and acceptance behave, before any storage or execution integration.**

Code: [`PlanContracts/PlanModel.cs`](../PlanContracts/PlanModel.cs), [`PlanContracts/PlanRules.cs`](../PlanContracts/PlanRules.cs).
Tests: [`PlanContracts.Tests/`](../PlanContracts.Tests/). They need no database:
`dotnet test context-store/PlanContracts.Tests/PlanContracts.Tests.csproj`.

## Scope of increment 2a

The increment contains:

- immutable snapshot types
- pure validation
- derived readiness and roll-up
- state-changing operations that return a new snapshot

It deliberately does **not** contain any of the following. Those are later increments and need a live database to verify:

- database schema
- an Npgsql service
- HTTP endpoints
- AGE projection
- an execution-store link

PlanRules has no I/O, no clock and no random identifiers. Every operation key, attempt id, actor, artifact and evidence reference is an input, so the same inputs always give the same result.

Nothing here claims database atomicity or durability. Integration must provide those, and they are listed under *Integration requirements*.

## Model

| Concept | Representation |
|---|---|
| Plan | A snapshot (`PlanGraph`) rooted at one node of type `plan`. The root's parent, if any, must be outside the snapshot. |
| Structure types | Existing node types only: `plan`, `plan_phase`, `plan_step`, `task`, `milestone`. No new types. |
| Hierarchy | `PlanNode.ParentId`, ordered by `SiblingOrder` then id. |
| Container / leaf | The root and any node with children is a **container**. Every other node is a **leaf**. A childless `plan_phase` is a leaf. |
| Dependencies | `Dependency(Predecessor, Successor, Gate?)`, separate from the hierarchy. They may cross containers within the same project. |
| Work state | `NodeState`, stored for **leaves only**. Containers may have a state row only for revision and operation-key bookkeeping. |
| Readiness | Always derived, never stored. |
| Contract version | `PlanContract.Version = "plan-contract/v1"`. |

### Two revisions, deliberately separate

- **`PlanNode.ContentRevision`** (≥ 1) is a counter that the **store** increments when a node's *specification* changes: requirements, acceptance criteria or inputs. Presentational edits such as name, sibling order or display text do not change it.
  - Adding or removing a child does not change a container's ContentRevision.
  - Acceptance decisions are bound to this revision.
  - It is a counter, not a hash, so consumers compare it as a number and never recompute it.
- **`NodeState.StateRevision`** (≥ 0) is the compare-and-set token. Every applied operation on a node increments it, including the one that records an acceptance. That is why accepting never makes its own approval stale.

### Attempt fencing

`NodeState.AttemptEpoch` is monotonic. Each start or reopen issues `epoch + 1`, and the epoch is never reset.

- Finishing or releasing work must present **both** the current attempt id **and** the current epoch.
- Cancelling or releasing clears the attempt id but keeps the epoch.
- So a late result from a released or cancelled attempt can never match a later run, even if the caller reuses the same attempt id.
- Only the latest epoch is stored, not a growing history of attempt ids.

### Artifacts and evidence

- `ArtifactRef` is an opaque, **immutable** content identity, such as a commit SHA, a content hash or a document revision id. Rules compare it with exact ordinal equality.
- Work may be completed without an artifact. A metadata-only task should record its output document's revision as its artifact.
- **Accepting** requires an artifact. Every decision, accept or reject, requires an `EvidenceRef`.

## Gates

A gate policy is explicit. Each plan has `DefaultGate`, which is `Accepted` for the development pilot, and each dependency may override it.

A predecessor **leaf** satisfies a gate only if it passes these checks in order. The first failing check is reported as the blocker reason.

1. It is not cancelled → otherwise `predecessor_cancelled`.
2. It has no **current rejection** → otherwise `predecessor_rejected`.
   - A rejection is current while it applies to the current attempt epoch and artifact.
   - A content revision alone does **not** lift a rejection; new work, meaning a new attempt, does.
3. Its work is `Done` → otherwise `predecessor_not_completed`.
4. Its own gates still hold, which makes staleness **transitive** → otherwise `predecessor_upstream_changed`.
5. Under an `Accepted` gate, its effective acceptance is `Accepted` → otherwise `predecessor_acceptance_stale` or `predecessor_not_accepted`.

A predecessor **container** passes these checks in order:

1. Its acceptance is not `Rejected` → otherwise `predecessor_rejected`. This takes precedence over incompleteness.
2. It is `Complete` → otherwise `predecessor_not_completed`.
3. Every descendant leaf's gates still hold → otherwise `predecessor_upstream_changed`.
4. Under an `Accepted` gate, its derived acceptance is `Accepted` → otherwise `predecessor_not_accepted`.

A leaf is gated by the dependencies declared on itself **and on every ancestor**. Blockers are ordered by owner, nearest first, and then by predecessor id.

### Readiness lifetime

Gates are **enforced** when work starts or reopens, and when work is accepted. After a node has started, gates are **reported** continuously instead of enforced:

- `LeafReadiness.GatesHold` is false when any gate on the node or its ancestors is unsatisfied.
- `UpstreamChanged` is true when `GatesHold` is false and the node is `InProgress` or `Done`.
- Work whose upstream changed is not cancelled automatically, but it cannot be accepted (`gates_not_satisfied`). It can still be rejected.

Because a predecessor whose own gates fail does not satisfy its successors, the effect spreads downstream. For example, in A → B → C → D, reopening A blocks C and D. Adding an unsatisfied dependency to accepted work has the same effect.

## Effective acceptance

`EffectiveAcceptanceOf` returns:

- `None` when there is no decision.
- `Accepted` or `Rejected` when a decision exists **and** all of these hold:
  - the node is `Done`
  - the decision's content revision equals the node's current one
  - its artifact equals the current artifact
  - its attempt epoch equals the current epoch
- `Stale` otherwise. The record is kept as history.

Consequences:

- Reopening a node makes its acceptance `Stale`.
- Finishing a new attempt does not revive the old approval, even with the same artifact, because the epoch changed.
- A stale acceptance never satisfies an `Accepted` gate.

## Work transitions (leaves only)

| From → To | Requirements | Effect |
|---|---|---|
| Todo → InProgress | attempt id; gates satisfied (`not_ready` with blockers otherwise) | issues epoch+1 |
| Done → InProgress (reopen) | attempt id; gates satisfied | issues epoch+1; acceptance becomes Stale |
| InProgress → Done | current attempt id **and** epoch | sets artifact (may be null) |
| InProgress → Todo (release) | current attempt id **and** epoch | clears attempt id |
| Todo / InProgress / Done → Cancelled | — | clears attempt id (fences the worker) |
| Cancelled → Todo (restore) | — | — |

Every other pair is rejected with `invalid_transition`. That includes same-status requests, Todo → Done and Cancelled → Done. `Done` and `Cancelled` are terminal unless explicitly reopened or restored.

## Containers (derived only)

Completion and acceptance are derived separately, over **all** children:

- **Completion:**
  - `Complete` when every child is complete (a leaf is `Done`; a container is `Complete`).
  - `Incomplete` otherwise.
  - `Empty` only for a root with no children.
- **Acceptance:**
  - `Rejected` when any child has a current rejection.
  - `Accepted` when the container is complete and every child is accepted with its gates holding.
  - `Pending` otherwise.
- **No silent descope.** A cancelled child keeps its container incomplete. An explicit, reviewed descope operation is future work.
- Containers cannot be transitioned or decided (`container_state_is_derived`).
- Their content cannot be revised (`container_revision_unsupported`) until container-level review semantics exist. This avoids letting changed requirements pass silently.

## Operations, idempotency and failure semantics

Operations: `AddDependency`, `RemoveDependency`, `Transition`, `Decide`, `ReviseContent`. Reads: `ValidateGraph`, `Evaluate`, `EffectiveAcceptanceOf`.

- Every operation takes an `OperationContext(OperationKey, ExpectedStateRevision, Actor)` for one node: the successor, for dependency operations.
- **No partial application.** On any rejection the result's `Graph` is the input instance itself, and the tests assert this by reference. Inputs are never mutated.
- **Order of checks.** Checks run in this order:
  1. unknown enum input
  2. a whole-graph validation (an invalid input gives `invalid_graph` plus the details)
  3. operation key and actor
  4. node exists
  5. idempotent replay
  6. state revision
  7. state-revision exhaustion
  8. the operation's own rules, including content-revision and attempt-epoch exhaustion
- **Idempotency.**
  - Operation keys are scoped per node, and only the latest key per node is kept, so memory stays bounded.
  - Each operation also stores a length-prefixed fingerprint of its whole payload, including actor and evidence.
  - Replaying the latest key with the same payload returns `Unchanged` and the input graph, even with an old expected revision.
  - The same key with a different payload is rejected with `operation_key_reused`.
  - An older key is treated as a new operation and normally fails `stale_revision`.
- Repeating an identical decision gives `Unchanged`. A changed decision, for example a reversal, is applied.
- Successful operations increment only the target node's `StateRevision`.

## Stable error codes

These codes are part of the contract surface. `ErrorCodeCoverageTests` proves that each one is reachable.

| Code | Raised when |
|---|---|
| `missing_root` | snapshot root id not among the nodes |
| `invalid_root_type` | root is not a `plan` node |
| `invalid_root_parent` | root's parent is inside the snapshot |
| `node_key_mismatch` | dictionary key differs from the node's id |
| `cross_project` | a node belongs to another project |
| `invalid_node_type` | a node type is not a plan-structure type |
| `missing_parent` | a non-root node's parent is absent |
| `hierarchy_cycle` | parent links loop without reaching the root |
| `orphan_state` | a state row for an unknown node |
| `invalid_revision` | content revision < 1, or state revision / epoch < 0 |
| `invalid_enum` | an undefined gate, work status or decision value (in the graph or as input) |
| `invalid_state` | an impossible state shape, any of: in-progress or done without an attempt id/epoch; todo/cancelled holding an attempt; a container holding work state; an acceptance with a bad revision/epoch; a stored decision missing its decider, evidence or attempt id; a stored *accepted* decision with no artifact; a current-epoch decision naming a different attempt. Loaded snapshots must meet the same contract `Decide` enforces. |
| `missing_dependency_endpoint` | a dependency references a node outside the plan |
| `self_dependency` | predecessor = successor |
| `duplicate_dependency` | the same predecessor/successor pair is declared twice |
| `dependency_on_ancestor` | a dependency between a node and its own ancestor or descendant |
| `dependency_cycle` | dependency edges form a cycle |
| `hierarchy_dependency_deadlock` | dependencies plus containment can never complete |
| `invalid_graph` | an operation's input snapshot failed validation (details follow) |
| `node_not_found` | the operation's target node is absent |
| `invalid_operation_key` | blank operation key |
| `actor_required` | blank actor |
| `operation_key_reused` | the node's latest key was reused for a different payload |
| `stale_revision` | expected state revision ≠ current |
| `revision_exhausted` | a state revision, content revision or attempt epoch is already `long.MaxValue` (no overflow; the input is returned unchanged) |
| `dependency_not_found` | removing a dependency that does not exist |
| `container_state_is_derived` | a transition or decision on a container |
| `container_revision_unsupported` | `ReviseContent` on a container |
| `invalid_transition` | a work transition outside the table |
| `attempt_required` | start/reopen without an attempt id |
| `stale_attempt` | finish/release/decide with an attempt id or epoch that is not current |
| `not_ready` | start/reopen while gates are unsatisfied (blockers returned) |
| `not_completed` | a decision on work that is not `Done` |
| `stale_content` | the reviewed content revision ≠ current |
| `stale_artifact` | the reviewed artifact ≠ current |
| `artifact_required` | accepting work that has no artifact |
| `evidence_required` | a decision without an evidence reference |
| `gates_not_satisfied` | accepting work whose upstream gates no longer hold (blockers returned) |

Blocker reasons are listed in reporting precedence: `predecessor_cancelled`, `predecessor_rejected`, `predecessor_not_completed`, `predecessor_upstream_changed`, `predecessor_acceptance_stale`, `predecessor_not_accepted`.

## Increment 2b1: PostgreSQL store and opt-in local API

**Code:**
- [`PlanContracts/PlanStore.cs`](../PlanContracts/PlanStore.cs)
- [`PlanStoreSchema.cs`](../PlanContracts/PlanStoreSchema.cs)
- [`PlanContractGate.cs`](../PlanContracts/PlanContractGate.cs)
- [`PlanContentDigest.cs`](../PlanContracts/PlanContentDigest.cs)
- [`Api/PlanContractEndpoints.cs`](../Api/PlanContractEndpoints.cs)

**Live tests:** [`PlanStore.LiveTests/`](../PlanStore.LiveTests/). Run with
`HEKATE_PLAN_LIVE_CONNSTR="Host=127.0.0.1;Port=5434;Database=postgres;Username=postgres;Password=postgres" dotnet test context-store/PlanStore.LiveTests`.
Each run creates and drops its own `hekate_plan_live_*` database. Without the variable the suite fails rather than skipping.

### Opt-in

Nothing is created unless `HEKATE_PLAN_CONTRACT=1`. With the flag set, all of the following must hold, or startup **fails before any managed DDL**:

- `HEKATE_API_URLS` is exactly one loopback URL (no wildcard, `0.0.0.0` or multiple hosts);
- `CODESTORAGE_CONNSTR` names exactly one loopback `Host` and a `Database`;
- `HEKATE_DISABLE_DISPATCHER=1`.

The schema also requires the AGE graph `code_graph`. The local launcher passes the flag only when `HEKATE_LOCAL_PLAN_CONTRACT=1`. Production/NSSM never sets it.

### Schema

The schema is additive and leaves `nodes` unchanged.

- `managed_plans`: root, project, default gate (`accepted`), contract version.
- `plan_node_state`: one row per managed node, holding the content revision, state revision, work, attempt id/epoch, artifact, the acceptance stamp, and the latest operation key and fingerprint. The row exists if and only if the node is managed.
- `plan_dependencies`: predecessor, successor, gate.

### Operations

The operations are: create a **new** managed plan (idempotent on root id + key + payload), add a child, add or remove a dependency, transition, decide, and revise content. Each runs in **one transaction**:

1. `SET LOCAL hekate.plan_contract='on'`
2. per-project advisory lock
3. whole-plan snapshot load
4. contract-version check
5. the PlanRules operation, including whole-graph validation
6. compare-and-set row updates
7. dependency diff
8. AGE reconcile in the same transaction
9. commit

Any failure rolls the whole operation back.

### Projection

The plan's dependencies are projected as `DEPENDS_ON {plan_root}` edges between `CodeNode {node_id}` vertices. The projection is identity only: no names or content.

- A convergent reconcile merges vertices, deletes stray or duplicate tagged edges, creates missing ones, and verifies the result.
- If AGE is broken, plan writes fail with `projection_failed`. That availability coupling is deliberate: the projection never drifts.
- `POST plans/{root}/project` repairs the projection.
- Legacy untagged edges and the legacy outbox are untouched.

### Write fence

Triggers raise SQLSTATE `HP409` (`managed_plan_protected`), which the API returns as HTTP 409 on every endpoint. A write is blocked when it:

- changes `value`, `node_type`, `project_id`, `parent_id` or `file_id` on a managed node, or deletes one;
- creates a structural node anywhere under a managed node, including via an unmanaged intermediary;
- turns an unmanaged node inside a managed tree into a structural type;
- moves any node into or out of a managed tree;
- writes any attribute on a managed node except the presentation keys `priority`, `target_date`, `display_color` and `notes` (including moving an attribute between nodes);
- writes the contract tables directly;
- truncates while managed plans exist.

This covers the HTTP endpoints, repositories, MCP raw SQL and seeders.

**Trust boundary:** any holder of full database credentials can `SET` the flag or disable triggers. This is an integrity fence, not authentication.

### API

`/api/plan-contract/v1`, loopback callers only:

- `POST plans`
- `GET plans/{root}` (nodes with content, dependencies, readiness)
- `GET plans/{root}/readiness`
- `POST plans/{root}/project`
- `POST nodes/{parent}/children`
- `POST nodes/{id}/dependencies`
- `DELETE nodes/{id}/dependencies/{pred}`
- `POST nodes/{id}/transition`
- `POST nodes/{id}/decide`
- `PUT nodes/{id}/content`

Every precondition is **required**; an omitted field is 400 `missing_field`. Status codes:

| Status | Codes |
|---|---|
| 400 | invalid input or enum |
| 404 | not found |
| 409 | `stale_revision`, `stale_content`, `operation_key_reused`, `revision_exhausted`, `plan_exists`, `node_exists`, `concurrent_modification`, `managed_plan_protected` |
| 422 | other rule codes, with blockers |
| 503 | `projection_failed` |

Enum values are snake_case names.

`attemptId` is an opaque attempt correlation, not an identity or auth principal.

### Deferred

- adopting or enrolling legacy plans (would lose their historical meaning)
- execution integration and the authoritative execution ledger
- UI
- auth

## Integration requirements (remaining)

1. **Storage.** Done in 2b1 (above). The AGE projection is reconciled in the same transaction instead of through the legacy outbox.
2. **Legacy writers.** All of them are inventoried in [013](013-plan-writer-inventory.md). Since 2b1 the database fence blocks every one of them from changing managed plans. **Migrating** legacy clients (`plan_sync.py`, `context_bridge.py`, the UI's generic node editor and MCP tools) to the contract API, or retiring them, is deferred.
3. **Execution link.** Execution records link to plan nodes by stable node id, never by title.
   - The authoritative execution ledger is **not** decided.
   - `Odin/run_hekate.py` and `gods/engine.py` use Postgres when a DSN is set and fall back to SQLite otherwise.
   - The engine import was restored by recovery commit `a1f8237`, which brought back `athena_complete`, `sessions` and `conversation`. Imports and targeted tests are verified; pipeline **activation** is not.
   - Which ledger is authoritative, and how its states map to `WorkStatus`, needs an explicit adapter decision.
4. **Snapshot loading:** implemented in 2b1 (`PlanStore.LoadSnapshot`). Mutation snapshots load under the project lock; read-only snapshots use a repeatable-read transaction. Loading keeps the stored root parent without normalising it, and loads incomplete stored decisions as values that validation rejects.
5. **Durability, concurrency and AGE projection:** live-tested in 2b1. Covered: concurrent compare-and-set (exactly one applies), opposing concurrent dependencies (exactly one gets `dependency_cycle`), AGE failure rollback, idempotent reconcile, the fence, and global id conflicts. Node deletion of managed nodes is fenced; no contract delete operation exists yet.

## Known limitations (v1)

- There is no container-level review; container acceptance is derived only.
- There is no explicit descope operation, so cancelling a child keeps its container incomplete.
- Each node remembers only one operation key for replay.
- Adding a child to a leaf that already has work state makes the snapshot invalid (`invalid_state`). Restructuring started work needs an explicit future operation.
- Nodes are created only through `CreatePlan` (new roots) and `AddChild`. **Reparenting and deletion are not supported**: the fence blocks them for managed nodes, and no contract operation exists yet.
