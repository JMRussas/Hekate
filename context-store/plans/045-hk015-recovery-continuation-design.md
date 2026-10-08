# Plan 045: Recovery and continuation of a stopped node (HK-ISSUE-015)

**Status: DESIGN ONLY, revision 3.1 (root msgs 2127, 2187, 2194; revised after root reviews 2200, 2205 and 2211).** The implemented primitive (feat/guarded-takeover `f6cdb97`) is the epoch/ref-only takeover; the manifest-aware API below is a FUTURE proposal, not in scope. Nothing here is implemented, authorized or applied. The historical check-002 run stays stopped and unchanged: no apply, no reset to `todo`, no disposition. Every claim below cites the source it rests on, at Hekate `2066614`. HK-ISSUE-015 stays open.

## 1. Problem

check-002 (root GO 2063) stopped `needs_operator` / `worker_failed` / `worktree_add_failed` in round 2:
- its round-2 journal stream ends at `dispatch_intent`;
- `run.json` lists `dispatch_intent@4` as an open intent;
- PlanStore keeps the node `in_progress` on the round-2 attempt, still carrying round 1's `artifactRef` as history.

Today nothing can close that intent truthfully, and nothing can continue the node without either granting a fresh round budget or misrepresenting history (HK-ISSUE-015). Two capabilities are needed, and they must be designed together:
- **R. Reconcile**: close an uncertain pre-launch dispatch with an evidence-backed resolution.
- **C. Continue**: run a node again under a bound that counts every earlier attempt, binding predecessors across runs.

## 2. Contracts this design relies on (facts)

| Fact | Source |
|---|---|
| `dispatch_intent` is journaled before the worker is called | `pilot.py`, `rec.exec_id = self.aj.dispatch(...)` before `self.worker(...)` |
| `launch_intent` is appended (committed, own transaction) before `Popen`; an append failure raises before `Popen` | `cli_worker.py` `CliWorker.__call__` |
| Pre-launch returns: `executable_hash_mismatch`, `adapter_needs_journal`, `worktree_exists`, `worktree_add_failed`, `worktree_head_mismatch`, `prepare_failed` | `task_runner.run` worker wrapper, `cli_worker.py` |
| Pre-launch LOCAL effects are possible: `git worktree add` (+ registration), `npm ci` in it (prepare hook) | `cli_worker.py`, `task_runner.install_deps` |
| The only terminal resolution for an open intent is `operator_resolution` with `decision` in `{confirmed_released, confirmed_finished, abandoned_no_effects}` and a non-empty `reconciliationRef` | `evidence.py` `TERMINAL_RESOLUTIONS`, `is_terminal_resolution` |
| Streams are writer-owned; appends from another writer are `foreign_writer` | `durable.py` append/resolve |
| `operator_takeover` bumps the writer epoch and inserts a `takeovers` row EVERY call (not idempotent) | `durable.py` `operator_takeover` |
| Every append re-checks epoch + same-session advisory lock under `FOR UPDATE` | `durable.py` `_check_fence` |
| `open()` takes the writer's advisory lock; `fence_busy` if a live session holds it | `durable.py` `DurableJournal.open` |
| Writers are per-process random ids (`coordinator#…`, `plan-cli#…`) | `local_store.py` `session`, `plan_cli.py` |
| The local store has a single-instance lock (`store_in_use`) | `local_store.py` |
| Operator acts log `intent` before and `outcome` after the HTTP call, with FRESH ids; an intent without an outcome blocks `LocalStore.session` (`uncertain_operator_acts`) | `operator_acts.py` `_act`, `uncertain_acts`; `local_store.py` `session` |
| PlanStore replays an identical (operationKey, fingerprint) on the node's LAST operation as `Unchanged`; a reused key with another fingerprint is refused | `PlanRules.cs` `Prologue` |
| `in_progress → todo/done` is attempt-fenced (attemptId, epoch, executorRef); every transition is state-revision-fenced | `PlanRules.cs` `Transition`, `Prologue` |
| `ArtifactRef` is set only on `in_progress → done`; nothing clears it | `PlanRules.cs` `Transition` |
| A non-`done` node's old decision is not effective (`Stale`) | `PlanModel.cs`, `PlanRules.cs` `EffectiveAcceptance` |
| `classify` stops `inflight` for any `in_progress` node, before any node-root check | `plan_run.py` `classify`, `run_plan` |
| A node root holds ONE task_runner run; an existing node root stops `node_run_root_exists` | `plan_run.py` |
| `bind_predecessor` requires exactly one `pilot-*/evidence.json` in `<run_root>/<pk>` | `successor.py` |
| Plan identities are uuid5 of the plan file: a new run root re-attaches to the same nodes | `plan_import.py` `identities`, `import_plan` |
| `maxRounds` is per task_runner run; nothing enforces a bound across runs (the attempt epoch only records) | `task_spec`, `pilot.py`, `plan_run.py` |
| Runner provenance is host-observed metadata only; it is NOT an ordering or no-spawn proof | `provenance.py` (HK-ISSUE-016) |

## 3. Truthful resolution semantics (revision 2, root review 2200)

Revision 1 proposed `abandoned_no_effects`. That **overclaims**: the proof cannot establish that the code which ran had the append-before-spawn ordering (provenance is host-observed metadata, HK-ISSUE-016), nor that no out-of-band process acted. This design therefore does NOT use `abandoned_no_effects` for this case.

What CAN be established under the fence (§4 M1):
- **(A)** No `launch_intent`, act or terminal record exists in the stream as of the fenced re-read.
- **(B)** No later append by the old writer can ever commit (epoch fence).
- **(C)** Under the reviewed adapter ordering, a spawn needs a committed `launch_intent`. That ordering is NEVER attested by a source pin: a pin is host-observed and does not prove which loaded code executed, or in what order (root 2205). (C) stays UNVERIFIED unless independent runtime-order evidence exists, and no such evidence exists today.

Proposed vocabulary change (needs root approval; touches `evidence.py`):
- **A new terminal decision `closed_under_fence_effects_unattested`.** Required fields:
  - `reconciliationRef`;
  - `attested`: a list naming exactly (A) and (B);
  - `unattested`: a list that ALWAYS includes `executed_adapter_ordering` and `out_of_band_activity`. `executed_adapter_ordering` may be removed only by independent runtime-order evidence, never by a source pin.
  It closes the stream (no further journaled effect is possible) without asserting that past unjournaled effects are absent. Readers must treat the attempt's effects as UNKNOWN beyond the attested items.
- **Stronger attestation (optional, later):** only independent RUNTIME-order evidence (not a source pin or provenance file) could move `executed_adapter_ordering` to `attested`. None is designed here; `out_of_band_activity` stays unattested regardless.

`abandoned_no_effects` stays reserved for cases where no-effects is actually proven (none today for this path).

## 4. The four mechanics

### M1. Fence: an authoritative guarded takeover (one transaction)

Revision 1's "read a helper, then call the unmodified takeover" is not atomic. Replacement (revision 3, root 2205): `operator_takeover_guarded(dsn, writer, ref, expected_epoch, expected_manifest_sha256, now)` in ONE transaction, taking the SAME locks in the SAME order that every journal append (including stream creation in `_append_tx`), `resolve` and compaction take: `global_usage FOR UPDATE`, then `writer_usage FOR UPDATE`. No create, append or resolve of any stream can interleave.
1. Lock `global_usage`, then the writer's `writer_usage` row.
2. If a GUARDED RECEIPT for (writer, ref) exists: if it binds the same expected_epoch and manifest and the current epoch equals its `to_epoch`, return it (replay, no write); a different binding → `takeover_ref_bound_elsewhere`; a moved epoch → `epoch_moved_after_takeover`. A replay returns the HISTORICAL effect and is decided BEFORE any manifest comparison: streams that the new-epoch writer changes afterwards never turn a replay into a refusal (root 2211). The reconcile step separately re-reads the CURRENT stream under the lock.
3. (First call only, no receipt yet) recompute the writer's STREAM MANIFEST under that lock: every stream with `writer_id = writer`, sorted by (root, claim_key), with its state, count, head hash and resolved_at, as canonical JSON. Its sha256 must equal `expected_manifest_sha256` (from the proof), else refuse `stream_manifest_changed` BEFORE any epoch change.
4. Else require `epoch == expected_epoch` (CAS), else refuse `epoch_moved_foreign`.
5. Atomically: bump the epoch, insert the legacy `takeovers` audit row, insert the guarded receipt (referencing that audit row), commit.

**Schema (additive, disposable databases first):** a NEW append-only table `supervisor_journal.guarded_takeovers` (`writer_id`, `reconciliation_ref`, `expected_epoch`, `to_epoch`, `manifest_sha256`, `takeover_id` referencing `takeovers.id`, `at_json`), with PRIMARY KEY (`writer_id`, `reconciliation_ref`) and the same UPDATE/DELETE/TRUNCATE guard as `takeovers`. Historical `takeovers` rows are never rewritten or deleted, and legacy duplicate refs there are irrelevant (no UNIQUE index on `takeovers`). The unguarded `operator_takeover` is unchanged. On a database without the table, the guarded call refuses `guarded_receipts_absent` (migrating the coordinator's existing store is a separate, reviewed step).

**Unknown commit outcome:** re-invoke with the same arguments. It either finds the row (done) or re-runs the CAS (not done). It never double-bumps.

**Other operator surfaces:** the unguarded `operator_takeover` remains for existing tests and tools. Any bump by it moves the epoch, so this command's CAS refuses `epoch_moved_foreign`. A foreign takeover can block this command but can never be silently absorbed by it.

**Sequence** (the command holds the local store's single-instance lock throughout):
- prove (read-only);
- write the immutable `proof.json` (`ref = sha256`);
- guarded takeover;
- `DurableJournal(writer).open()` (`fence_busy` → `writer_live`);
- fenced re-read: EXACTLY `[claim_intent, claimed, package_ref, dispatch_intent]`, or that plus our own resolution;
- append the resolution (§3) with a record id derived from `ref`;
- `resolve()`.

### M2. Takeover blast radius

A takeover fences every stream of the writer. The proof lists all of them. Apply refuses `other_streams_unresolved` unless each other stream is resolved, or is named in the GO as collateral. **Collateral is NEVER resolved by this command**; its uncertainty is preserved (root 2200).

### M3. Partial-apply replay

| Step | Replay rule |
|---|---|
| `proof.json` | exclusive write; equal bytes → resume; different → `proof_changed` |
| takeover | M1 (guarded receipt keyed by (writer, ref), manifest + CAS under the global journal lock) |
| resolution append | record id from `ref`; an unknown commit is resolved by reading the row by id before any resend (the existing `DurableJournal.unknown` path) |
| `resolve()` | no-op once resolved |
| PlanStore act (§5) | operationKey from `ref` + step; replay of the same key and fingerprint is `Unchanged` only if it is the node's last operation, so the command first reads the node and compares `LastOperationKey` |

**Operator-act log**, a new `classified` phase, approved only in this form (root 2200):
- it may classify ONLY an uncertain intent of THIS command (its id is recorded in `proof.json`; its operationKey is derived from `ref`);
- the classification requires an AUTHORITATIVE RECEIPT, either the PlanStore response to an identical replay (`Unchanged`, same fingerprint) or the node state read showing `LastOperationKey` = ours with the matching fingerprint;
- the `classified` line stores that receipt's digest;
- every other (foreign) uncertain intent keeps blocking `LocalStore.session`.

### M4. Local pre-launch leftovers

The proof lists `wt-rN`, its `.git/worktrees` registration, `node_modules` inside it and `refs/hekate-pilot/<run>/rN`, with their observed state. Nothing is removed. These are recorded as observed local state, not as "effects absent" (§3).

## 5. R: the reconciliation record

`<run_root>/<key>.reconcile-r<N>/proof.json` (`hekate-reconcile-prelaunch.v1`):
- the bound record shas;
- the root GO;
- the declared executing Hekate commit and its provenance status;
- the stream (root, claimKey, writer, kinds, last record hash, `expected_epoch`);
- all of the writer's streams;
- the PlanStore view (work, attemptId, epoch, executorRef, `stateRevision`, `artifactRef` plus its explanation from the last DONE round, `LastOperationKey`);
- the leftovers;
- the §3 `attested` / `unattested` lists.

R touches only the journal. PlanStore is unchanged, so plan runs keep stopping `inflight` until a disposition (§6).

## 6. C: disposition, a server-side attempt bound, continuation

**No historical disposition is authorized** (root 2200). Designs only:

- **Supersession (preferred over pretending `done`):** a dedicated PlanStore operation `supersede(node, {evidenceRef, supersededByArtifact?, reason}, expectedStateRevision)`.
  - Allowed from `in_progress` (attempt-matched), `todo` or `cancelled`.
  - It sets work `cancelled` and stores immutable supersession metadata on the node, never an acceptance.
  - `classify` reports `node_superseded` distinctly from `node_cancelled`. The decided acceptance and `artifactRef` history are untouched.
- **Attempt bound, enforced atomically IN PlanStore:**
  - Plan files gain an explicit, versioned per-node `maxAttempts` (a new `plan-import.v1`; v0 plans have NO bound and are never reinterpreted from a spec's per-run `maxRounds`).
  - At import, `maxAttempts` is persisted on the node as IMMUTABLE policy. A re-import with a different value for the same node is refused (`policy_mismatch`).
  - Plan identities are uuid5 of the plan file, so a different file is a different plan with its own nodes and its own GO. A caller cannot reset an existing node's budget.
  - `PlanRules.Transition` (`todo/done → in_progress`) refuses `attempts_exhausted` when `AttemptEpoch >= MaxAttempts`, inside the same state-revision-fenced operation. Client-side checks are only for messages.
- **Retry:** `in_progress → todo` (attempt-fenced) is offered by the tool ONLY for v1 nodes with a persisted policy and remaining budget. v0 nodes, including check-002, get no retry path.
- **Continuation layout:**
  - each new attempt run uses a NEW node root `<key>.a<epoch>/`;
  - `plan_run` chooses it from PlanStore's epoch;
  - predecessor selection binds the FULL identity, not just a commit. Exactly one run among `<pk>` and `<pk>.a*` whose:
    - evidence `specSha256` equals the node's pinned spec;
    - base equals that spec's `taskBaseCommit`;
    - run identity (runId, claimKey, attemptId, attemptEpoch) matches the attempt that PlanStore's accepted DECISION reviewed (`reviewedAttemptEpoch`, `reviewedArtifactRef`);
    - outcome is `accepted`;
    - run-owned ref resolves to that artifact.
  - Zero or several matches → `predecessor_evidence`.

## 7. Tests (offline: disposable harness, fake CLI, temp repos)

- **R1:** a pre-launch `worktree_add_failed` round → apply → stream closed `closed_under_fence_effects_unattested`, with exact `attested`/`unattested` lists; PlanStore unchanged; leftovers listed, not removed.
- **R2:** `launch_intent` present → `launch_seen`, no takeover.
- **R3 race:** an old-writer append after the takeover → `fenced:epoch`, no spawn.
- **R4:** a live writer → `writer_live` (the takeover stands, the stream is untouched).
- **R5:** a guarded takeover: replay returns the same epoch without a write; a concurrent identical call → one bump (serialized; the receipt PK); a stream created or resolved after the proof → `stream_manifest_changed` with no epoch change; a foreign bump between prove and takeover → `epoch_moved_foreign`; a simulated unknown commit → re-invoke, single bump.
- **R6:** a crash after each step → resume to an identical final state; a second run is all skip/`Unchanged`.
- **R7:** other unresolved streams → refused unless named; named ones stay unresolved.
- **R8:** `classified` only with a matching receipt for OUR intent; a foreign uncertain intent still blocks the session.
- **C1:** supersede → `cancelled` + metadata; `classify` → `node_superseded`; acceptance and `artifactRef` unchanged.
- **C2:** a v1 node at `maxAttempts` → PlanStore refuses the claim `attempts_exhausted` (two concurrent claimers: exactly one succeeds below the bound, none at it); a re-import with a changed `maxAttempts` → `policy_mismatch`; a v0 node → no retry offered.
- **C3:** predecessor selection: two runs with the same artifact but different spec/attempt → only the one matching the decision binds; no match → stop.

## 8. Root decisions (2200) recorded, and what remains

- **Recorded:**
  - no historical D1 or apply;
  - supersession metadata preferred;
  - `classified` only with a receipt for the command's own act;
  - no collateral resolution;
  - predecessor selection binds the full identity;
  - HK-ISSUE-015 stays open.
- **Remaining for root:**
  - (1) approve the §3 vocabulary (`closed_under_fence_effects_unattested`) or require stronger attestation first;
  - (2) approve the schema and PlanStore changes (the additive `guarded_takeovers` table, node `maxAttempts` policy, `supersede`, `plan-import.v1`);
  - (3) the order of the increments.
- **Proposed order:** M1 guarded takeover + the vocabulary (journal only) → R → supersede → the v1 attempt bound → retry + layout + predecessor binding. Each increment gets its own frozen source, review and GO.
