# Plan 012 — Plan node contracts v1 (`plan-contract/v1`)

**Status: increment 2a implemented (pure rules + tests only). Not wired to any store, API or UI.**
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

## Integration requirements (not done; need review and a live database)

1. **Storage.** The existing Postgres is the authority. AGE is only a projection: today `AgeLayer.CreateEdge` writes to `graph_sync_outbox`, and the worker supports only `sync_vertex` and `create_edge`. Proposed tables:
   - `plan_dependencies`
   - `plan_node_state`
   - a content-revision column on `nodes`

   Each mutation would run in one transaction:
   1. take a per-project advisory lock
   2. load the snapshot
   3. run PlanRules
   4. compare-and-set the state revision
   5. write the changes plus an outbox row
   6. commit

   Removing a dependency needs a new `delete_edge` outbox operation. All of this must be verified against a real database.
2. **Every legacy writer must be inventoried before integration.** That means more than `NodeService.UpdateNodeFull` and `UpdateAttributes`: it includes `NodeRepository` updates and attribute writes, seeders, the decomposer, and Python syncs (`plan_sync.py`, `context_bridge.py`). For each one, decide whether it changes content (increment ContentRevision), changes state, or is presentational. Bumping two methods is not enough.
3. **Execution link.** Execution records link to plan nodes by stable node id, never by title.
   - The authoritative execution ledger is **not** decided.
   - `Odin/run_hekate.py` and `gods/engine.py` use Postgres when a DSN is set and fall back to SQLite otherwise.
   - The engine import is currently broken on this branch.
   - Which ledger is authoritative, and how its states map to `WorkStatus`, needs an explicit adapter decision.
4. **Snapshot loading** must map database rows into `PlanGraph.Create` and reject duplicate ids, which `Create` already does.
5. **Durability, concurrency and AGE projection** claims need live tests: concurrent compare-and-set, the advisory lock preventing concurrent cycles, outbox draining, and the cascade on node delete.

## Known limitations (v1)

- There is no container-level review; container acceptance is derived only.
- There is no explicit descope operation, so cancelling a child keeps its container incomplete.
- Each node remembers only one operation key for replay.
- Adding a child to a leaf that already has work state makes the snapshot invalid (`invalid_state`). Restructuring started work needs an explicit future operation.
- Reparenting and node creation or deletion are not operations in this slice. Snapshots containing them are validated as a whole.
