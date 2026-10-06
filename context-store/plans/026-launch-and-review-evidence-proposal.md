# Plan 026 — E2: durable launch and review-pending evidence (proposal)

**Status: design-only proposal, revision 5 (folds in codex-hekate msgs 853, 856, 859, 862, 868 and 871). No implementation GO.** No journal, schema, service, provider, heartbeat or wake mechanism is activated or claimed by this document.

**Builds on:**
- [023](023-coding-worker-adapter-proposal.md): ownership and the E1 split;
- [024](024-supervisor-e1a-validation.md) and [025](025-supervisor-e1b-validation.md): the test supervisor, fault outcomes, H1 interop;
- [019](019-durable-claims-and-pins.md): claims, pins, receipts, attempt events;
- [018](018-claim-receipts-design.md) §4: leases, still deferred.

**Labels:** **V** = verified in source at `6f50dac`; **I** = inferred.

## 0. Problem

E1a/E1b proved the supervisor's *policy*: uncertain outcomes stop for a human, nothing is retried, and a replay is never permission to relaunch. That evidence lives **in memory** (`SupervisedRun`, `scripts/local/supervisor_e1/e1/supervisor.py`). A real supervisor that crashes loses:
- which claim it was requesting;
- whether it started an effect;
- what the worker produced;
- whether a finish was sent;
- who was asked to review.

E2 decides **what execution evidence must be durable, where, and how a human reconciles it**, without a second task-state ledger.

## 1. Source facts

| Fact | Source |
|---|---|
| **V:** PlanStore is the single task truth: node work, attempts, pins, acceptance, append-only attempt events, immutable claim receipts | [019](019-durable-claims-and-pins.md); `PlanStoreSchema.cs` |
| **V:** Each audited state change appends an event with `operationKey`, `actor`, `nodeStateRevision`, `kind`, attempt, epoch, artifact, executor reference and pins. It is readable via `GET nodes/{id}/events` | `PlanStore.AppendEvents`, `PlanContractEndpoints` events view |
| **V:** Every applied operation on a node increments its `stateRevision` (structural operations included); dependency and structural operations emit no event | 012 "Successful operations increment only the target node's StateRevision"; 016 (no events for structural operations) |
| **V:** Transition idempotency is **last-operation only** (`Prologue` compares `LastOperationKey`). `LastOperationKey` is **not** exposed in the public plan view | `PlanRules.cs` `Prologue`; `PlanContractEndpoints.View` (no `lastOperationKey` field) |
| **V:** Review facts come from the view: `work`, the raw `acceptance`, `effectiveAcceptance` (`none`, `accepted`, `rejected`, `stale`), pins, and readiness (`gatesHold`, blockers) | `PlanContractEndpoints.View` and `ReadinessView` |
| **V:** The E1 fault outcomes keep evidence in memory only. The E1a/E1b replay helper's "provably last operation" check compares view state, revision and artifact (a **conservative heuristic**, section 5 C7) | `e1/supervisor.py` `replay_held_finish` |
| **V, anti-pattern:** gods startup `recover_stuck_tasks` resets `running` to `pending` with a retry, an automatic re-execution | `Odin/gods/engine.py:539` |
| **V, anti-pattern:** Hermes relay events are best-effort and swallow errors | `Odin/gods/handlers/hermes_async.py:632-635` |
| **V, observed:** delivery is not consumption, and consumption is not action. Persisted, delivered bridge messages were not acted on by an agent whose turn had ended. **No supported mechanism exists to wake an ended turn** | bridge msg 686 |

## 2. Principles

1. **One task ledger.** PlanStore alone answers task-state questions. The journal answers only "what did this supervisor intend or do, and what does it not know?".
2. **Intent before effect.** Every external effect (**claim POST**, spawn, finish transition, notification send) is preceded by a durable intent record carrying the held key and payload. An intent without an outcome means *unknown*, never *did not happen*.
3. **Unknown means manual.** There is no automatic relaunch, retry, release or ownership transfer, and no automatic backup assignee.
4. **Candidate, not completion.** A worker result captured before a PlanStore-confirmed finish is candidate evidence. A worker that finished while its PlanStore finish is uncertain stays **unknown**, whatever was delivered.
5. **Acknowledgment and progress are recorded acts.** Delivery and reading are transport.
6. **Bounded, without ever blocking evidence.** Caps refuse *new* work; they never prevent recording the outcome of work already started (section 6).

## 3. Where each fact lives

| Fact | Durable home |
|---|---|
| Claim, attempt, epoch, pins, executor reference | PlanStore (receipt and attempt events) |
| Done, artifact, acceptance; the exact finishing operation key, actor and revision | PlanStore (state and attempt events) |
| Review **candidates** and **accept-eligibility** | **Derived** from the PlanStore view (section 7), never stored |
| Claim intent, launch, exit, captured result (candidate), finish intent and outcome | **Supervisor journal**: execution evidence only (section 4) |
| Review request, acknowledgment, progress checkpoints, operator resolutions | Supervisor journal: workflow evidence (where it ultimately lives is open question 3) |
| Notification send and dedup | Journal intent and outcome. Delivery itself is transport-owned |

## 4. Journal: stream identity and record shapes (shape only; no schema)

**Stream.** The primary key is **`(rootId, claimKey)`**, known **before** the claim request. The attempt id is caller-held. The **epoch is unknown until a receipt arrives** and is added by the `claimed` record. Each stream has exactly one **writer**: the supervisor instance whose id appears on every record (writer provenance). Another instance never writes to it (section 5, "foreign").

**Every record carries:**
- `seq` (monotonic within the stream);
- `at` (clock labelled; open question 2);
- `writer` (supervisor instance id);
- correlation identities as they become known: package token, `suppliedSha256` plus the three instruction strings (025), and the content and prerequisite digests verbatim.

| Record | Written | Content |
|---|---|---|
| `claim_intent` | **before** `POST claims` | root, claimKey, attemptId, executorRef, actor: the exact request payload |
| `claimed` | after the claim reply is parsed | receipt identities including the **epoch**, `replayed`, `stillCurrent` |
| `launch_intent` | **before** spawn | `launchKey = launch:<claimKey>:<epoch>`, workspace reference, command identity (no secrets), budget |
| `launched` | after spawn returns | process-handle facts and the owned process-group identity (E3) |
| `exited` | after exit | `exitCode`, `timedOut`, `killed`, duration, output digests and sizes (no blobs) |
| `result_captured` | after parsing | typed outcome, `artifactRef`, structured-result digest: **candidate** |
| `finish_intent` | **before** the transition | held `operationKey`, payload, `expectedStateRevision` |
| `finish_outcome` | after the reply, or after reconciliation | `applied`, `unchanged`, `rejected:<code>`, or the result of the proof in C7 |
| `review_requested` | after an **authoritatively confirmed Done** (C8), candidates only | responsible lead, attempt, artifact, receipt reference |
| `review_acknowledged` | an explicit lead act | lead, attempt, artifact |
| `review_progress` | an explicit lead act | **monotonic checkpoint id** plus an evidence reference or finding (section 7) |
| `notify_intent` / `notify_outcome` | around each send | channel, **dedup key** (`notify:<claimKey>:<epoch>:<purpose>:<n>`), transport message id, `delivered`/`failed`/`unknown` |
| `operator_resolution` | an operator act | decision (`release`, `confirm_finished`, `abandon_candidate`, `new_claim_permitted`), reason, `reconciliationRef` |

## 5. Crash points and reconciliation facts

"Last durable record" means what was in the stream when the writer died. Every resolution is an **operator act**.

| # | Last durable record | Unknown | Reconciliation facts | Allowed resolution |
|---|---|---|---|---|
| C0 | none | Nothing was attempted for this stream | — | Nothing to reconcile |
| C1 | `claim_intent` | whether the claim committed | `GET claims/{root}/{claimKey}`; attempt events carrying that claim key. **A 404 only means "not observed at this read": the original request may still commit** | **Found:** a **replayed** claim (no dispatch); release or abandon. **404:** the claim stays **unknown** until there is **confirmed server-side completion** of that request or transaction, or an operation-specific reconciliation by an operator. **The client process or connection being gone is not quiescence**, because the server may continue a request after a disconnect. Elapsed time or a timeout is never proof. **A 404 is never a reason to recommend a fresh key.** Any later claim under any key still treats a late commit of this key as a replay |
| C2 | `claimed` | whether anything started | node view (InProgress, this attempt); no `launch_intent` | Nothing was launched by this writer. Release |
| C3 | `launch_intent` | spawn and effects | owned process group (E3); workspace and git state | Launched with unknown effects: inspect, reconcile, record `operator_resolution` |
| C4 | `launched` | exit, effects, result | process alive?; workspace diff | As C3. Killing an orphan is an operator act (E3) |
| C5 | `exited` | parsed result | exit facts; output digests | Non-success or unknown: release after inspection. Never finish from the exit code alone |
| C6 | `result_captured` | whether a finish was attempted | the candidate; node still InProgress? | The candidate may support a finish only under a **new** `finish_intent` after the operator confirms preconditions. The node stays unknown until then |
| C7 | `finish_intent` | whether **this** finish committed | **Durable proof from a coherent snapshot.** The event page and the node view come from **independent** API endpoints today, so reading both is **not** one atomic snapshot, and a timestamp label does not make it coherent. E2a gathers these facts inside a **fixture-only database repeatable-read snapshot**. Future production proof needs an explicitly reviewed coherent-read mechanism; without one, the outcome is `proof_missing`. Within such a coherent snapshot: The node's events must contain an `attempt_finished` event that matches **every** held fact: `operationKey`, actor, root, node, attempt id, epoch, executor reference, artifact, content revision and pins (`attemptContentRevision` and `attemptPrereqDigest`), all as the held payload and package state them. Its `nodeStateRevision` must also equal the **current** node `stateRevision` in the same snapshot. Every later operation on the node bumps the revision, so this proves the held operation was the last one **at that snapshot**. Any later write is still guarded by a compare-and-set | **Proved:** record `finish_outcome=applied`, labelled with the snapshot. **Matching event exists but the revision moved:** applied, then superseded; reconcile from PlanStore. **No matching event, node InProgress at the expected revision:** `finish_unconfirmed`. This is **not** proof that the finish did not happen: the pending request may still commit. No resend without confirmed server-side completion of that request, or an operator decision. A client disconnect or a timeout is not completion. **Anything else (or partial fact match):** `intervening_mutation` or `proof_missing`; never resend |
| C8 | an **authoritatively confirmed Done** for this attempt | whether review was requested | candidate classification (section 7) | Write `review_requested` for **candidates only**, idempotent by attempt. "Confirmed Done" means a PlanStore view showing Done for this attempt **and** one of: `finish_outcome=applied`, a reconciled `unchanged` or C7 proof, or `operator_resolution=confirm_finished`. A finish reply alone is never enough (msg 859) |
| C9 | `notify_intent` | delivery | the transport's record for the dedup key | Re-send only under the **same dedup key**, at most K times (section 6). Never reassign |
| C10 | delivered, not acknowledged | whether anyone holds the review | fake-clock age; no PlanStore decision | Bounded escalation **to the same responsible lead** or into a visible operator queue. **No automatic ownership transfer or backup assignee** |
| C11 | acknowledged | progress | `review_progress` checkpoint ids and their evidence; PlanStore decision | Only a **new** checkpoint id with **new** evidence or a new finding resets the deadline (section 7) |

**C7 note: the E1 heuristic.** The E1a/E1b `replay_held_finish` decides "our finish is the last operation" from **view** state:
- `work`, attempt, epoch, artifact and executor reference;
- `stateRevision == expected + 1`.

This does **not** prove the same operation key, because the public view has no `LastOperationKey`. Its scope is the narrow **tested scenario**: a **conservative preflight** in which the server's compare-and-set is the final authority.
- When it wrongly says "ours", a resend is still refused server-side (`stale_revision` or `operation_key_reused`), so it can never cause a wrong write.
- It is **not** reconciliation proof, and it does not establish permanent "guaranteed unchanged" idempotency. The acceptance owner recorded that clarification in [025](025-supervisor-e1b-validation.md).

**Source note for future durable replay:**
- Any replay or reconciliation decision across a restart **must** come from exact audit correlation: every held fact listed in C7, plus `nodeStateRevision` against the current revision, within one **coherent** snapshot (a fixture-only database repeatable-read snapshot in E2a; a reviewed coherent-read mechanism in production, or else `proof_missing`).
- When that correlation cannot be established (events missing, ambiguous or out of reach), the outcome is **`proof_missing`**, which means manual reconciliation. It is never inferred from view state.

## 6. Bounds and retention

- **Per stream:** at most N records (proposed 64), with **reserved slots**. Before any effect intent (`claim_intent`, `launch_intent`, `finish_intent`, `notify_intent`), the stream must have room for that effect's terminal records (`claimed`, `exited`, `result_captured`, `finish_outcome`, `notify_outcome`) plus **at least two** `operator_resolution` and fault records. If it lacks room, the **intent is refused**. A cap can therefore never prevent recording what happened to an effect already started.
- **Byte caps:**
  - references (≤ 512 B);
  - notes and findings (≤ 4 KiB);
  - command identity (≤ 2 KiB);
  - digests are fixed-size;
  - **no blobs**.
- **Per writer, unresolved:** at most M unresolved streams (proposed 32). Over the cap, new `claim_intent` is refused (fail closed). Unresolved evidence is **never** evicted.
- **Retention:** a *resolved* stream may be compacted to a summary after D days (proposed 30). Resolved means a confirmed Done plus a PlanStore decision, or an `operator_resolution`. The summary keeps identities, outcome, resolution and `reconciliationRef`. Unresolved streams are never compacted.
- **Global cap on everything retained (msgs 859 and 868):** one count cap and one byte cap per writer (proposed S records and B MiB) cover **all** retained data:
  - active streams;
  - resolved but not yet compacted streams;
  - summaries.

  Summaries also have a retention period (proposed R days).
  - **Admission:** before a new `claim_intent`, the writer must have room in the global caps for that stream's full **reserved terminal capacity** (the per-stream reservation above).
  - **Making room:** the only eligible evictions are the oldest **resolved** data (summaries first, then compaction of resolved uncompacted streams). Their PlanStore facts (events, receipts, decisions) remain the durable record.
  - **If eviction cannot free enough room**, the claim is **refused** (fail closed).
  - Unresolved data is never evicted. Growth is bounded at every stage.
- **Notifications:** dedup key per purpose; at most K sends per review (proposed 3); then a visible operator queue. **"Repeat notifications are harmless" is not assumed:** the bounded dedup and its evidence are required.

## 7. Review candidates vs accept-eligibility (derived; no second ledger)

| Done leaf, effective acceptance | Meaning | Classification |
|---|---|---|
| `none` | Never reviewed | **Review candidate** |
| `rejected` | A recorded, terminal review outcome | **Not pending.** Rerun (reopen, new attempt) or replan is an operator decision |
| `stale` | An earlier decision no longer matches content, artifact, attempt or prerequisites | **Operator classification:** re-review vs rerun/replan. Not a blanket candidate |
| `accepted` | Current acceptance | Not pending |

**Accept-eligible** is a separate derived subset of candidates. It holds only while everything `decide(accepted)` re-checks (019 §1a) currently holds:
- the current attempt, artifact and content revision match what will be reviewed;
- the attempt's pins hold (no content or prerequisite drift);
- the gates hold.

The derivation exposes **two** lists, *candidates* and *accept-eligible*, and stores neither. A worker that finished while its PlanStore finish is uncertain (C6/C7) is in neither list: it is **unknown**.

**Workflow evidence:**
- **Responsible lead** (`review_requested`): a named lead for a candidate attempt. It becomes moot when the node stops being a candidate, derived again and never stored as "closed".
- **Acknowledgment** (`review_acknowledged`): an explicit lead act. It is distinct from delivered, read, or decided.
- **Progress** (`review_progress`): it needs a **monotonic checkpoint id** plus an evidence reference or a concrete finding.
  - A repeated checkpoint id, **a new checkpoint id carrying the same evidence reference or finding**, or an unchanged note, does **not** reset the review deadline, so a "heartbeat" cannot hold a review open indefinitely.
  - A PlanStore decision ends the review.
- **No wake mechanism is claimed.** Delivery cannot resume an ended turn (msg 686). Practical review flow therefore needs an **active watcher** that derives candidates from PlanStore and escalates (open question 5). E2 does not provide one.

## 8. First experiment (E2a): test-only conceptual model

**Question:** For each crash point C0–C11, can an operator classify the situation correctly from the durable facts the model says would exist (model journal prefix plus PlanStore), with no automatic action?

**Explicit non-claims:**
- E2a makes **no durability, restart, fsync, atomicity or wake guarantee**.
- Its "prefix" tests are conceptual: they show *which facts suffice*, not that any store preserves them.

**Shape:**
- **Journal hooks are injected into the existing E1 test supervisor** (`e1/supervisor.py`), not an independent copy. The hooks are an optional `journal` callback called at each section 4 point, defaulting to a no-op so the E1a/E1b suites are unchanged.
- An in-memory model journal collects the records. A "crash" is simulated by keeping only a prefix and discarding the supervisor instance.
- A **fake clock** and an explicit **wake callback** drive escalation and progress deadlines.
- It runs against the real API on a new disposable database (E1a harness). There is no process launch: C3 and C4 are records only (E3).

**Checks:**
1. **Ordering:** for every scripted path, each effect's intent precedes the effect, starting with `claim_intent` before `POST claims`. The effects are claim, (modelled) spawn, finish and notify.
2. **Classification:**
   - A pure `classify(prefix, planstore_view, events, receipt)` returns each row's unknowns and allowed operator resolutions. It never returns an automatic action.
   - C1 is reproduced with and without a committed claim.
   - C7's sub-cases are reproduced through **real** API states, using the **audit-event proof** (every held fact + `nodeStateRevision == current`). The facts are gathered inside a **fixture-only database repeatable-read snapshot** (direct SQL in the test harness), not through two independent API reads; this is not a production mechanism:
     - proved;
     - applied then superseded;
     - unconfirmed;
     - intervening.
   - The E1 heuristic is shown to be conservative, never wrong-writing, but not proof.
3. **Candidates vs accept-eligibility:**
   - Both are derived from the real view across decide (accepted, rejected), reopen, cancel, revise and upstream drift.
   - `none` is a candidate; `rejected` is not pending; `stale` is "operator classification".
   - Accept-eligibility drops with pin or gate drift while candidacy remains.
   - An uncertain finish is in neither list.
4. **Workflow:**
   - Delivered but unacknowledged stays unacknowledged.
   - Escalation goes to the same lead or the operator queue, bounded at K, with no transfer.
   - A repeated `review_progress` checkpoint id does not reset the fake-clock deadline; a new one with evidence does.
5. **Bounds:**
   - Reserved slots: an intent is refused when the stream lacks room for its terminal and fault records, and the terminal records of started effects always fit.
   - Byte caps are enforced.
   - The per-writer unresolved cap refuses `claim_intent`.
   - Compaction never touches unresolved streams.
6. **Foreign streams:** a second writer given another writer's unresolved prefix refuses to act on that claim.
7. **Restart replay (conceptual):** classification rebuilt from the prefix alone matches the pre-crash classification, with no durability claim.

**Exit:**
- Checks 1–7 pass with database-verified no-write assertions.
- The outcome is a reviewed record shape and classification table.
- An actual durable journal (storage choice, schema, fsync and atomicity: **E2b**) needs its own proposal and GO.

## 9. Not included

- Any durable journal, schema, file format, storage choice or service.
- Process launch, owned-group cleanup and cancellation (E3).
- Leases and heartbeats (3b2).
- Provider or worker activation.
- Auth.
- Any notification-transport or wake mechanism.

## 10. Open questions

1. Stream key: `(rootId, claimKey)` with single-writer provenance (proposed), or per writer instance?
2. Clock for `at` and the deadlines: the database clock (018's rule: sampled once after the lock) or local monotonic plus wall clock? E2a uses a fake clock.
3. Where `review_requested`, acknowledgment and progress ultimately live: a separate append-only workflow table beside PlanStore (not task state) or the supervisor journal.
4. The default values of N, M, D, K, S, B and R, and the byte caps.
5. Which **active watcher** derives candidates and escalates in practice: a supervisor loop, a scheduled job or a human-run check? No supported wake mechanism exists today.
6. Should PlanStore expose the node's last operation key (read-only) so that C7 proof needs no event scan? That would be a contract addition, outside E2.
