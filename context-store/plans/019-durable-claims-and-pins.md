# Plan 019 — Durable claims and attempt pins (increment 3b1, exact scope)

**Status: accepted by codex-hekate after independent source review and 172 pure / 48 live / 59 HTTP checks. Implemented by claude-hekate on 2026-10-06; validation evidence in [020](020-durable-claims-validation.md).**

**Decisions already made by codex-hekate (msg 553):**
- Prerequisite invalidation is **strict**.
- Increment 3b1 contains **only** durable claims and pins.
- Leases, heartbeats, expiry and reclaim are **deferred to 3b2**.

Attempts stay InProgress until an explicit release or cancel. No workers, no execution activation, and no changes to Odin, auth, UI or the launcher.

It builds on [012](012-plan-node-contracts-v1.md), [016](016-attempt-provenance-audit.md) and [018](018-claim-receipts-design.md), whose open items are resolved here as listed.

## 1. Pins on every start path (pure rules)

- `NodeState` gains two optional members, appended last: `AttemptContentRevision` (`long?`) and `AttemptPrereqDigest` (`string?`).
- **Start and reopen** (generic `Transition → InProgress`, and claims, which use the same rule) set:
  - `AttemptContentRevision` = the node's current content revision;
  - `AttemptPrereqDigest` = `PlanRules.PrerequisiteSnapshot(graph, leaf).Digest` (section 3). The digest excludes the target's own state, so it is the same before and after the transition.
- **Finish** (`InProgress → Done`) checks, in order:
  1. Pins are NULL (a legacy attempt): rejected with `stale_content` (fail closed).
  2. The current content revision differs from `AttemptContentRevision`: rejected with `stale_content`.
  3. The current `PrerequisiteDigest` differs from `AttemptPrereqDigest`: rejected with **`stale_prerequisites`**, a new error code (HTTP 409).
- **Done keeps the pins**, recording what the finished work was built against. **Release and cancel clear** the pins with the attempt. Reopen sets new ones.
- **Legacy attempts** (InProgress with NULL pins, from before 3b1) can be read, released, cancelled, and replayed with their exact old key, because the replay check in Prologue runs first. They **fail closed on finish**. Nothing is backfilled.

### 1a. Pins also guard acceptance (codex-hekate msgs 556 and 557)

- **A new `Decide(Accepted)` on a pinned Done attempt** is checked in this order:
  1. the Prologue replay check (an exact latest-key replay is still `Unchanged`);
  2. the existing checks (`not_completed`, `stale_content` / `stale_artifact` / `stale_attempt` on the reviewed tuple, evidence, artifact);
  3. **`gates_not_satisfied`**, which keeps its existing precedence when gates are currently blocked;
  4. **the pins:** a current content revision different from `AttemptContentRevision` gives `stale_content`; a current prerequisite digest different from `AttemptPrereqDigest` gives `stale_prerequisites`.

  Own-content or prerequisite drift after the finish can never be accepted by going around the pins. Re-executing needs an explicit reopen.
- **`Decide(Rejected)`** stays allowed with the exact reviewed tuple and evidence; rejecting stale work is always legitimate.
- **Legacy Done with NULL pins:** it can be read, and its existing acceptance record is preserved. A **new Accepted decision fails closed** with `stale_content`, because the legacy artifact's provenance is unknown. A new Rejected decision is allowed.
- **`EffectiveAcceptanceOf` with pins:** a **pinned** acceptance record (one whose attempt has non-NULL pins) is `Stale` when the node's current content revision differs from the pinned one, or its current prerequisite digest differs from the pinned one. This applies even if the newly added prerequisites are currently green. **Legacy acceptance records** (NULL pins) keep the 2a/2b1 meaning; they are never silently reset.
- **Existing records are never rewritten.** Only the derived effective acceptance changes.
- **No claim permission:** a matching pin grants no new claim or authority.

### 1b. Compatibility: an intended strengthening of the contract

Two 2a behaviours become stricter. This is intended, and the tests change accordingly:

- `State_only_operations_do_not_stale_acceptance` (adding a dependency after acceptance) now **stales** a pinned acceptance, because a dependency input was added. A presentation-only or irrelevant change still does not stale it.
- `Upstream_change_after_start…`: the **finish** is now rejected with `stale_prerequisites`. A separate test keeps the old guarantee: a finish *before* an upstream change still has acceptance blocked by `gates_not_satisfied`.
- `ValidateState`: pins must be NULL when `AttemptId` is NULL; both pins are set together (content revision >= 1) or both NULL, so a partial pair loads as `invalid_state`; containers must have NULL pins.
- The transition fingerprint is **unchanged**: pins are derived, not payload. 2b1 and 3a retry keys stay compatible.

## 2. Content snapshot

The receipt stores the node's own content exactly as granted:

- `content_revision`;
- `content_digest` (`PlanContentDigest` of the value and content attributes);
- `content_snapshot` as jsonb: `{value, attributes}`, with only the content keys.

Finish pinning uses the content revision; every content change goes through the contract and bumps it. The digest and snapshot are evidence of what the worker was given.

## 3. Prerequisite snapshot algorithm

`PlanRules.PrerequisiteSnapshot(PlanGraph g, Guid leaf)` is implemented **inside** `PlanRules` on its private `Index`. It reuses `OwnersOf`, `ChildrenOf`, the declared dependencies and `IsContainer`, so there is no second gate engine. It reads **raw** state only, and the digest is memoised per node within one `Index`, since pin-aware effective acceptance needs it. It runs on a valid graph only and returns a canonical, ordered structure plus `digest = SHA-256(domain "hekate-prereq/v1" + length-prefixed canonical encoding)`.

1. **Work-node chain.** `owners = OwnersOf(leaf)` (the leaf, then its ancestors up to the root). For each owner, record `(ownerId, nodeType, parentId)` in chain order. The owners of every upstream node visited in step 3 follow, sorted by id (msg 563: upstream ancestor-chain identity). The owner walk is cycle-guarded and fails closed.
   - This pins the work node's identity and its whole parent chain: a reparent, which is fenced anyway, or a type change would show up.
   - The leaf's own **state** and **content** are **excluded**. Content is pinned separately (section 2), and its own start, finish and decide must not invalidate it.
2. **Declared gates of the chain.** For each owner in chain order, its declared dependencies sorted by predecessor id, as `(ownerId, predecessorId, effectiveGate = d.Gate ?? DefaultGate)`. Added or removed edges, and changed gates (per-edge or the plan default), change this list.
3. **Closure of relevant upstream nodes.** A worklist, starting from every predecessor found in step 2, with a visited set:
   - **Leaf predecessor `p`:** record `leaf(p)` = `(id, parentId, nodeType, contentRevision, work, attemptId, attemptEpoch, artifactRef, the FULL raw acceptance tuple (decision, contentRevision, artifactRef, attemptId, attemptEpoch, decidedBy, evidenceRef) or ∅ (msg 563), p's own pins (AttemptContentRevision, AttemptPrereqDigest) or ∅)`.
     - **Only raw state facts are recorded. It never calls the pin-aware `EffectiveAcceptanceOf`,** which itself depends on prerequisite digests, so there is no circular hashing or readiness.
     - The recursive closure already captures every input that `p`'s effective acceptance depends on.
     - Then add `p`'s own gating chain: for each owner of `OwnersOf(p)`, record that owner's declared dependencies (as in step 2) and enqueue their predecessors.
     - This is transitive because the readiness rules treat a predecessor whose own gates fail as unsatisfied (`predecessor_upstream_changed`).
   - **Container predecessor `c`:** record `container(c)` = `(id, parentId, nodeType, contentRevision, child ids sorted by id)`. Names and sibling order are presentation and are not pinned (msg 561), so membership changes stale the digest but reordering does not.
     - Then enqueue **all of its children**, recursively. Descendant leaves get `leaf(...)` records, and their gating chains are traversed exactly as above.
     - This is because container completion, acceptance and `GatesHold` derive from every descendant leaf and that leaf's gates.
4. **Not included:**
   - the leaf's unrelated siblings, and any node not reached through steps 1–3;
   - **raw `StateRevision`** and operation-key bookkeeping. Only semantic state fields are pinned, so another node's replay or no-op bookkeeping cannot invalidate this work. The trade-off is documented.
   - **Consequence:** an independent claim on a sibling, or work elsewhere in the plan, never stales this attempt.
5. **Canonical encoding.**
   - Sections in a fixed order: `chain`, `declared` (all recorded `(owner, predecessor, gate)` triples, deduplicated, sorted by `(ownerId, predecessorId)`), `nodes` (all `leaf` and `container` records, sorted by id, each tagged with its kind).
   - Every field is length-prefixed, with null as `~`. Enums use stable snake_case names (an undefined enum value throws and fails closed). Numbers use the invariant culture. The digest is lowercase hex SHA-256.
   - The domain tag `hekate-prereq/v1` is part of the hashed bytes, so a future algorithm change is a new tag, never a silent reinterpretation.

**Strictness:** any change to a relevant predecessor's content revision, work, attempt, artifact or acceptance, or to a relevant edge or gate (including inherited and transitive ones, and container membership and descendants), changes the digest and makes finish fail with `stale_prerequisites`. The worker must release and start again with fresh inputs.

## 4. Claims (store and API)

### Table `plan_claim_receipts`

- Append-only, with the same guards as `plan_attempt_events`: UPDATE, DELETE and TRUNCATE are always rejected with HP409, even with the store flag; INSERT requires the flag.
- Primary key `(root_node_id, claim_key)`.
- Columns:
  - `request_fingerprint`: length-prefixed `{actor, attemptId, executorRef-or-~}`, with no lease fields;
  - `outcome`: `claimed` | `no_ready_work`;
  - `node_id`, `attempt_id`, `attempt_epoch`, `executor_ref`;
  - `content_revision`, `content_digest`, `content_snapshot jsonb`;
  - `prereq_digest`, `prereq_snapshot jsonb` (the canonical structure, for display);
  - `event_seq` (the `attempt_started` event), `actor`, `created_at`.
- `plan_attempt_events` gains a `claim_key` column, set on the `attempt_started` event of a claim. There is no backfill.

### `POST /api/plan-contract/v1/plans/{root}/claims {claimKey, attemptId, executorRef?, actor}`

One transaction: fence, per-project lock, then:

1. **An existing receipt at `(root, claimKey)`.**
   - Same fingerprint: **replay**. Return the original receipt, with `replayed: true`, and a **factual** `stillCurrent` computed read-only from current state.
   - No state, event or projection writes happen, and no other claim is made, **even if the plan has since changed or become invalid** (then `stillCurrent` is false).
   - A different fingerprint: 409 `operation_key_reused`.
2. **A new request.** Validate the graph and the contract version: an invalid graph gives 422 `invalid_graph` and an unsupported version gives `unsupported_contract_version`. Then take `PlanRules.Evaluate(g).ReadyWork.First()` (hierarchy order).
   - **None:** insert a `no_ready_work` receipt. Response 200 with `stillCurrent=false` and `current=null`. A later request needs a **new** key.
   - **A leaf:**
     1. Apply `PlanRules.Transition(start)` with the internal operation key `hekate-claim:<claimKey>`.
     2. Persist the state.
     3. Write the `attempt_started` event with `claim_key`.
     4. Insert the receipt with its snapshots.
     5. Reconcile the projection.
     6. Commit, **atomically**. Any failure leaves no receipt, event, state change or projection change.
- **Key namespace:**
  - Generic operations reject operation keys that start with the reserved prefix `hekate-claim:` (`invalid_operation_key`, in both the store and the API).
  - A claim key is never compared with generic last operation keys; receipts are looked up by `(root, claimKey)` only.
- **`stillCurrent`:** the node is InProgress, the attempt id and epoch equal the receipt's, `AttemptContentRevision` and `AttemptPrereqDigest` equal the receipt's pins, the current content revision equals the pin, and the current prerequisite digest equals the pin.
  - It is false for `no_ready_work`.
  - It is factual correlation only, **not** authority. It does not make external effects safe (see 018 §1).

### `GET /plans/{root}/claims/{claimKey}`

- Returns the receipt plus `stillCurrent`.
- 404 `plan_not_found` or `claim_not_found`.

### Limits and errors

- `claimKey`: 1–128 characters of `[A-Za-z0-9._~-]` (the URL-unreserved set, so the GET path needs no escaping), and never exactly `.` or `..` (dot segments are normalised away in URIs). Ordinary keys containing dots are allowed. The reserved prefix cannot occur because `:` is outside the alphabet. The same rule is a CHECK on the table. `attemptId` and `actor`: 1–256, not blank. `executorRef`: as in 016. A violation gives 400 `invalid_input` or `invalid_executor_ref`, and no receipt.
- Status codes: 400, 404, 409 (`operation_key_reused`, `revision_exhausted`, `stale_prerequisites`), 422 (`invalid_graph`, `unsupported_contract_version`), 503 (`projection_failed`). Responses use stable codes only.

### Implementation notes (review conditions as built)

- **Order inside the claim transaction:** fence → project lock (taken before any claim choice or write) → read the receipt → replay, or load + version + validate → choose → write. Replays and GETs **never** write state, events, receipts, `event_seq` or AGE.
- **Replay correlation fails closed:** the receipt is read before the graph is loaded. `stillCurrent` is then computed separately; an unreadable plan (unknown stored enum), an invalid graph or an unsupported contract version gives `stillCurrent=false` while the original receipt is still returned.
- **`stillCurrent` also compares content:** besides the revision pins, the receipt's `content_digest` must equal the digest of the node's CURRENTLY saved content (msg 563). A raw trusted repair that changes the value without bumping the revision therefore shows `stillCurrent=false`. This is detection for well-behaved paths, not protection against database credentials.
- **`no_ready_work` is a write:** it validates the graph and version like a claim, reconciles the projection, and an AGE failure leaves no receipt.
- **Reserved prefix (store-side):** a generic operation whose key starts with `hekate-claim:` is rejected with `invalid_operation_key` **unless** that key is the target node's current `LastOperationKey`. That exception is the exact latest-key replay, which the rules then treat as usual (`Unchanged` with the same payload, `operation_key_reused` otherwise). `CreatePlan` rejects the prefix for new plans.
- **Receipt row integrity:** a CHECK makes every `claimed` field NOT NULL (except `executor_ref`) and every `no_ready_work` field NULL. A foreign key `(root_node_id, event_seq) → plan_attempt_events(root_node_id, seq)` ties a claim to its start event. Because of that key, a plain `TRUNCATE plan_attempt_events` is now refused by PostgreSQL (`0A000`) before any trigger runs; `TRUNCATE … CASCADE` reaches the append-only fence (HP409).
- **Pins on audit events (msg 569):** `plan_attempt_events` also gains `attempt_content_revision` and `attempt_prereq_digest` (nullable, no backfill). Started, reopened, finished and decided events record the current pins; released and cancelled events record the pins **before** they are cleared. A generic start therefore keeps its input provenance after a release.
- **Response:** `{contractVersion, replayed, stillCurrent, current: {work, attemptId, attemptEpoch} | null, receipt: {…, contentSnapshot, prereqSnapshot}}`. The snapshots are JSON with camelCase fields and snake_case enum names. `current` is null for `no_ready_work` or when the plan cannot be read.
- The node view and the event view also show the pins (`attemptContentRevision`, `attemptPrereqDigest`), and events show `claimKey`. No other endpoints were added.

## 5. Tests (required for acceptance)

**Pure:**
- Pins are set by start and reopen, kept on finish, and cleared by release and cancel.
- A legacy NULL-pin attempt fails closed on finish, but its release and exact replay are allowed.
- `stale_content` after a content revision.
- `stale_prerequisites` after each of these, in its own case:
  - a direct predecessor's content, work, artifact or acceptance changes;
  - an added edge on the leaf;
  - a removed edge;
  - an inherited ancestor edge or a gate change;
  - a transitive predecessor-of-predecessor change;
  - a container predecessor's descendant changes, or a child is added.
- **No** invalidation from:
  - an unrelated sibling's start, finish or acceptance;
  - the leaf's own start, finish or decide;
  - another node's operation-key bookkeeping.
- The digest is deterministic regardless of insertion order, and carries the domain tag.
- **Acceptance pins (1a):**
  - a new Accepted after own-content or prerequisite drift gives `stale_content` / `stale_prerequisites`, with `gates_not_satisfied` keeping precedence while gates are blocked;
  - Rejected is still allowed;
  - legacy NULL-pin Done keeps its historical acceptance, a new Accepted fails closed, and a new Rejected is allowed;
  - an exact latest-key replay of a decision is still `Unchanged`;
  - a pinned acceptance becomes `Stale` after prerequisite drift, even with new green prerequisites;
  - the snapshot uses raw acceptance only (no recursion through pin-aware acceptance).
- **Updated 2a tests (1b):** dependency addition stales a pinned acceptance; the upstream-change-after-start finish is rejected; finish-before-upstream-change still blocks acceptance.
- Fingerprints are unchanged when no reference is given.

**Live:**
- Concurrent claims with different keys get different leaves, or exactly one records `no_ready_work`.
- Concurrent requests with the **same key and payload** result in exactly one claim, and both responses return the same receipt.
- The receipt is replayed after a later finish, a release, and a new claim on another node: the original receipt is returned, no new claim is made, and `stillCurrent` reflects the change.
- `no_ready_work` stays stable after work becomes ready.
- A payload conflict gives 409.
- Event and receipt rollback on an AGE failure and on sequence overflow.
- Append-only guards on receipts.
- Reserved-prefix generic keys are rejected.
- An **actual schema upgrade** from the 3a shape: the columns and table are added, an existing in-progress attempt keeps NULL pins (fail closed on finish, release allowed), and there are no receipts or backfill.

**HTTP:**
- Claim, replay and conflict.
- `no_ready_work`.
- GET receipt, and 404.
- Input errors.
- A finish rejected with `stale_prerequisites` / `stale_content` gives 409.

## 6. Deferred (3b2 and later)

- Leases, heartbeats, expiry and reclaim (018 §4).
- Auth binding of claims to principals.
- An executor adapter.
- Executor workspace isolation and reconciliation.
- Worker activation.
- Migrating legacy NULL-pin attempts.

## 7. Known wire limit (msg 592)

Int64 counters (revisions, epochs, `seq`, `eventSeq`, `contentRevision`) are JSON numbers here and in the rest of the v1 contract. JavaScript `Number` is unsafe above 2^53−1. A future TypeScript adapter MUST parse them losslessly (or the wire contract moves to decimal strings), or reject unsafe counters before acting; it must never round. No serializer change is part of 3b1, and no adapter is activated.
