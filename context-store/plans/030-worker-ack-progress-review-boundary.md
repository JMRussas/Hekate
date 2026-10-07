# Plan 030 — Production boundary: worker execution acknowledgment, progress and the review obligation (design proposal)

**Status: design-only proposal, revision 7 (folds in codex-hekate reviews msgs 1105, 1112, 1145, 1148 and 1152, and the codex-chatagent consumer verdict msg 1124 as relayed by codex-hekate msg 1125; changes listed in §13–§18), for review by codex-hekate (lead) and codex-chatagent (consumer). No implementation GO.** Nothing here adds or changes a production schema, service, provider, bridge, CLI or wake mechanism, and nothing here is tested. Every proposed contract below needs its own review and GO.

**Task:** codex-hekate msg 1088. Consumer context: codex-chatagent msgs 1068, 1090 and 1096; ChatAgent C1a `98c3d12`.
**Base:** Hekate `d0ed671` (E2b-a committed). agent-bridge facts pinned to committed `7a33328` (§1.5).
**Builds on:**
- [023](023-coding-worker-adapter-proposal.md): supervisor ownership;
- [026](026-launch-and-review-evidence-proposal.md) rev 5: record shapes, crash points, review derivation;
- [028](028-e2b-durable-journal-proposal.md) / [029](029-supervisor-e2b-a-validation.md): durable journal rules, proven **fixture-only**.

**Labels:** **V** = verified in source at the commit given; **I** = inferred; **P** = proposed here.

## 0. Problem and scope

Today PlanStore answers *what is allocated and what was finished or decided*. Nothing answers:
- whether a worker actually started on its allocation;
- whether it is still making progress;
- whether a reviewer has taken up a finished candidate.

ChatAgent's read-only status therefore reports `executionAcknowledged: "unknown"` for every leaf (V, ChatAgent `src/integrations/hekate/devCoordination.ts:222, 349` at `98c3d12`). This proposal defines, as a **boundary** (ownership, identities, states, rules, bounds):
1. worker execution **acknowledgment** (ACK), distinct from claim allocation;
2. worker **progress**;
3. the **review obligation** after a confirmed Done: request, lead ACK, lead progress, moot;
4. which deadlines exist and how they survive restart;
5. what "delivered", "consumed", "acknowledged" and "progress" each mean;
6. who would own the production journal and the coherent read;
7. what wake capability actually exists.

**Not in scope:** implementing any of it, choosing production storage, a wake mechanism, and auth.

## 1. Source facts

### 1.1 PlanStore (task ledger)

| Fact | Source |
|---|---|
| **V:** The plan-contract API has plan create/list/view/readiness/project, node child/dependency/content edits, `transition`, `decide`, claims (`POST /plans/{root}/claims`, `GET …/claims/{claimKey}`) and event pages (plan and node). It has **no** endpoint for worker ACK, progress, review request or review ACK | `context-store/Api/PlanContractEndpoints.cs:37–153` |
| **V:** Attempt event kinds are exactly `attempt_started`, `attempt_reopened`, `attempt_finished`, `attempt_released`, `attempt_cancelled`, `work_restored`, `decision_recorded` and `content_revised` | `context-store/PlanContracts/PlanStoreSchema.cs:107–108` |
| **V:** A claim writes a durable receipt (root, claimKey, node, attemptId, epoch, executorRef, pins, actor) and an `attempt_started` event. Claiming is **allocation**: it records nothing about a worker process or session | `PlanStoreSchema.cs:136–158`; 019 |
| **V:** Review candidacy is derived (Done with `effectiveAcceptance = none`), never stored. `stale` needs whole-graph prerequisite pins | `PlanRules.cs:174–203`; 026 §7 |
| **V:** The API is gated to loopback (plan-contract gate) | `context-store/PlanContracts/PlanContractGate.cs:35–64` |

### 1.2 Journal and coherent read

| Fact | Source |
|---|---|
| **V:** The only durable journal is **fixture-only**: the `supervisor_journal` schema exists only in disposable `hekate_plan_e1_*` test databases. Nothing is in `PlanStoreSchema` or any production migration | `scripts/local/supervisor_e1/e1/journal_schema.sql`; 029 "Not claimed" |
| **V:** The E2b-a coherent read is a **shared-adapter experiment** using direct table access in one REPEATABLE READ snapshot. It is not a production contract. In-snapshot review classification cannot verify a standing decision's prerequisite pins (`decided_unverified`) | `e1/recovery.py` (`snapshot_review`); 029 §msg 941 |
| **V:** E2a vocabulary already has `review_requested`, `review_acknowledged`, `review_progress`, `notify_intent` / `notify_outcome` and `operator_resolution`. It has **no** worker-ACK or worker-progress kinds | `e1/evidence.py:18–28`; 026 §4 |

### 1.3 Wake: what exists

| Mechanism | What it is | Why it is not a supported wake for this purpose |
|---|---|---|
| `pg_notify('node_changed')` trigger | AFTER INSERT/UPDATE on the **legacy `nodes` table** (**V** `context-store/DbLayer/Schema.cs:126–137`) | NOTIFY is **not durable**: it is lost when no listener is connected. It is not specific to plan state. It carries only a node id |
| `AgentDispatcher` | `LISTEN node_changed`, then trigger rules from `tools/skills/triggers.json` **spawn new CLI agents**, debounced in memory for 30 s (**V** `context-store/AgentCoordination/AgentDispatcher.cs:1–130`). **Disabled** by `HEKATE_DISABLE_DISPATCHER=1` in the local plan-only profile (**V** `context-store/Api/Program.cs:624–644`) | It starts *new processes* from rules. It does not resume an existing session, has no durable delivery, and is off in the local profile |
| `/api/events` SSE | Pushes the in-memory `SystemMessageBus` to **currently connected** clients (**V** `Program.cs:575–603`) | Not durable; no replay; nobody is listening when a session has ended |
| Odin gods pipeline | A `tick_interval` polling loop over its own relay (**V** `Odin/gods/pipeline.py:215–226`) | Legacy engine on separate storage (CLAUDE.md), not wired to PlanStore |
| agent-bridge | A durable mailbox, live delivery and long-poll `/api/wait`, plus the mechanisms in §1.5 (explicit ACK, sessions, leases, work claims, an opt-in harness worker) | **Cannot wake an existing IDE/app turn** (V, `docs/wake.md:3–5, 30, 67` @`7a33328`; msgs 686, 880/881). Delivery, read flags and transport ACKs are transport facts. The harness worker **launches a new process**; it does not resume a session (§1.5). Observed this session: a watcher can miss messages while it is being re-armed |

**Conclusion (V+I): there is no supported wake capability that resumes an agent session or guarantees that a recipient acts. No existing IDE wake is proven** (msg 1125; the bridge itself states that no idle IDE wake was tested, `docs/wake.md:67`). This is a **required capability gap** (msg 1105 §7), not an optional feature. Until a supported wake is designed and accepted separately, every design below treats notification as **best-effort delivery of an intended message** plus a **durable obligation** that an *active watcher* re-derives (026 OQ5). It never treats notification as action.

### 1.4 ChatAgent C1a (`98c3d12`, read-only consumer)

| Fact (V at `98c3d12`) | Where |
|---|---|
| A single `GET /api/plan-contract/v1/plans/{root}` to a literal loopback host. No auth, **no writes**. It fails closed with error codes and bounds: a 4 MiB body, 10,000 nodes and a 10 s timeout (60 s maximum) | `devCoordination.ts:14–98, 397–473`; `scripts/devcoord.ts` |
| Leaf states: `ready \| blocked \| in_progress \| review_pending \| accepted \| rejected \| stale \| cancelled`. Done + `none` maps to `review_pending`; Done + `accepted` + `upstreamChanged` maps to `stale` | `devCoordination.ts:207–215, 377–394` |
| `executionAcknowledged` is hard-coded `"unknown"`. "a claim or in-progress state is allocation, not proof that a worker started". Bridge delivery is "never an acknowledgement" | `devCoordination.ts:9–11, 222, 349`; integration doc |
| Its roadmap names the next increment: "Hekate-owned durable execution acknowledgement and review progress, followed by a supported wake adapter" | `docs/12-development-roadmap.md` (C1a entry) |

### 1.5 agent-bridge transport and runtime mechanisms (msg 1125)

**Pinned to the committed tree at agent-bridge `7a33328`** ("Add advisory ownership and recoverable supervised harness workers", 2026-10-06), per msgs 1139 and 1141. Every **V** below cites a file that is unmodified in the working tree at the time of reading; `cli.py` and `README.md` are modified there and are not cited. Paths are relative to the bridge repo.

| Mechanism | Fact (V @`7a33328`) | Source | What it does **not** establish for Hekate |
|---|---|---|---|
| **Explicit ACK** | A message may be sent `ack_required`. Reads with `ack=explicit` only peek; ack-required mail is never consumed by a plain read. `acknowledge` sets a durable, idempotent `acknowledged_at` under `BEGIN IMMEDIATE`; a repeat returns the original timestamp. The recipient, its instances or the operator may ACK | `mailbox.py:45–46, 225–245, 335`; `server.py:393, 796, 823`; `docs/wire.md:23–24` | Only that a holder of the recipient's (or the operator's) credential called ACK. The bridge says it means "accepted responsibility, not completion or approval". It is **not** a Hekate `worker_ack` or `review_acknowledged` and anchors no Hekate deadline (§2.3) |
| **Sessions** | Insert-only rows (UUID, owner, `registered_at`, an allow-listed context with `harness`, `conversation_ref`, a self-reported `listener_connected`, `wake_capability ∈ {none, context_only, supervised_worker}`, `provenance`, `model_attested: false`). No lease, heartbeat, expiry or update. The directory's `listener_connected` counts open `/notify` sockets in this process only and resets on restart; `last_seen_s_ago` reflects recent send/inbox calls | `evidence.py:46–66`; `server.py:239–241, 275, 853, 897`; `mailbox.py:16–18, 198, 260, 286`; `docs/wire.md:53–54` | Liveness, attention or model identity ("Registration does not prove liveness"). A bridge session id is a candidate **input** to `workerSession` / `leadSession` (OQ2), not a binding (§4) |
| **Path leases** | Advisory; TTL 1–86400 s (default 900); conflict on any overlapping active path under `BEGIN IMMEDIATE`; renew only while active; released rows kept; wall-clock expiry can pass while the bridge is down | `leases.py:1, 29–32, 45–70, 96–98`; `docs/wire.md:107` | Fencing ("Expiry does not fence filesystem writes") or Hekate allocation. PlanStore claims stay the only allocation (§1.1) |
| **Durable work claims** | Keyed by an ack-required message id; actions `start`, `launched{pid}`, `completed{artifact_ref}`, `failed`, operator `reset`. `start` refuses a `running`/`completed` claim; no TTL, renewal or fencing token; survives restart | `runtime.py:1, 21–62` | A Hekate attempt, act or finish. `launched` is a pid; `completed` is a harness exit (`docs/wire.md:111, 115–116`) |
| **Opt-in harness worker** | Run manually (`python -m agent_bridge.worker … -- <argv>`, role token only, one per state dir). It long-polls `/api/wait?ack=explicit`, claims `start`, **launches a new process** with the message on stdin, records `launched{pid}`, and only after exit 0 records `completed` and then the transport ACK. Nonzero exit → `failed`, no ACK; timeout or post-launch error → `uncertain`, recovered by operator `reset` + `--recover`. No automatic install or restart | `worker.py:18–43, 75–92, 115–126, 138–174, 180–220`; `docs/wake.md:38–67` | Resuming an existing session or IDE turn ("does not inject input into an existing IDE"); implementation, tests or acceptance ("exit of zero … does not prove implementation completion"). A process launch is never a worker ACK (§2.3) |
| **Evidence / outcomes** | Append-only `sessions`, `links`, `outcomes` with UUID and `ts`, no revision. Reads are **unbounded** (all links/outcomes/events for a message, whole-table link scan) | `server.py:738–769`; `evidence.py:113, 143–163`; `docs/wire.md:68–69` | Verified evidence ("attributed claims; the bridge does not … independently verify"). Not a substitute for the bounded per-key C-B read (§8) |
| **Ordering** | Message `id` is a per-database autoincrement; there is no per-thread or per-message revision. Read state is overlaid from an in-memory map with a debounced write. "Explicit delivery may repeat" | `mailbox.py:93, 124, 141–148`; `docs/wire.md:18, 37` | Coherence: equal ids or read states across two reads say nothing about what happened between them (§2.8) |

**Uncommitted, not accepted (msg 1141):** the bridge working tree also holds unowned, uncommitted changes, including a modified `src/agent_bridge/cli.py` and an untracked `src/agent_bridge/launcher.py` (its header calls it a local stdio MCP entry point that stays usable while the HTTP bridge is down). Nothing in this plan relies on them, and they are not a wake mechanism.

**Use for Hekate (I):** E2c and any later adapter should **reuse** these mechanisms and their own tests as transport and launch plumbing, not reimplement them (msg 1124). Every Hekate effect still goes through §6.

## 2. Principles (026 §2, plus eight additions)

1. **One task ledger.** PlanStore alone holds task state. ACK, progress and review workflow are **evidence** and never change task state.
2. **Allocation is not execution.** A claim (receipt plus `attempt_started`) allocates an attempt. Only a recorded worker **ACK** with the exact identity says a worker took it up.
3. **Transport and launch facts are not acts** (msg 1125). *Delivered*, *consumed* and a **transport ACK** (the bridge's explicit message ACK, §1.3) come from the transport; a **process launch** (including the bridge's opt-in harness worker starting a CLI) comes from a launcher. None of them substitutes for a validated Hekate worker or review act. *Acknowledged* and *progress* are explicit recorded acts, each validated against **current** PlanStore facts (§6).
4. **Unknown means manual.** There is no automatic relaunch, reassignment, release, decision or deadline extension.
5. **Duplicates, conflicts and stale messages are inert.** An exact duplicate is a no-op; a different payload under the same ActId is a conflict. Neither, nor a stale or foreign act, ever advances state, resets a deadline, or grows storage beyond bounded bookkeeping and saturating counters.
6. **No wake is claimed, and expiry is advisory.** Obligations are durable and re-derived by an active watcher; notifications are intended messages. A missed deadline triggers no automatic action of any kind.
7. **Acts are attributed claims, not proofs.** `dispatch_intent` proves only the supervisor's intent to send. A worker ACK is the named session's attributed self-report that it took the work up. It is not proof that a process is alive or that any effect started.
8. **Independent reads are never coherent evidence** (msg 1125). Two reads that happen to return the same `stateRevision`, `snapshotRevision`, event `seq` or bridge message id are still two reads. Matching values do not prove that nothing changed between them or that an append happened inside either. Only route A (§6) is coherent.
9. **Only current, verified evidence advances current status** (msg 1125). Evidence that is stale, unknown, compacted, `not_observed`, `unverified` (route B) or incomplete never advances a current acknowledged, progress or review status, and never starts or resets a deadline. This is judged **per derived field, against that field's required proof** (§8 C-B, msg 1145). A missing, corrupt or unverified required proof makes **that** field `unknown`. A compacted or truncated record that is not required proof for a field does not affect it, and neither does an evidence page that continues.
10. **The plan survives evidence failure** (msg 1125). If the journal, C-B, the bridge or any evidence read is unavailable, slow, corrupt or incomplete, PlanStore task state and its read (C1a today) are unaffected. Evidence-derived status degrades to `unknown`; it never blocks, rewrites or hides plan state.

## 3. Ownership (P)

| Concern | Owner | Notes |
|---|---|---|
| Task state, allocation, finish, decisions | **PlanStore** (Hekate) | Unchanged |
| Review **obligation** (who must review what) | **Derived** from PlanStore + the `review_requested` evidence | Never stored as "open"/"closed" |
| Execution and workflow **evidence** (dispatch, delivery, worker ACK and progress, review request, ACK and progress, notifications, operator acts) | **Supervisor journal**, owned by the **Hekate supervisor** | Production storage **unselected** (028 OQ1). It needs its own proposal and GO; E2b-a does not select it |
| Coherent read of PlanStore facts | **PlanStore**: a read-only snapshot contract (P, §8 C-A) | Covers PlanStore rows only. Direct table access stays fixture-only |
| Validating an act against current facts and recording it | **Supervisor**, under §6 step 6 | Only route A (§6: one reviewed transaction in the same database, OQ1) or an explicit operator act makes an act **effective**. Under route B an act is an audit-only observation. Independent reads are never presented as one snapshot |
| Delivery and read flags | **Transport** (agent-bridge today) | Advisory evidence only |
| Status presentation | **ChatAgent** (read-only consumer) | Reads PlanStore and, in future, the evidence read contract (§8 C-B); never writes |
| Operator resolutions, writer takeover | **Operator** | 026/028 rules |

## 4. Identities (P)

Every inbound act must carry its **full** key. A partial key is **rejected** (malformed); it is never matched loosely.

| Key | Fields | Source of each field |
|---|---|---|
| **AttemptKey** | `rootId, nodeId, attemptId, attemptEpoch` | The PlanStore claim receipt |
| **ExecutionKey** | AttemptKey + `claimKey` + `runId` + **`packageRef`** + `workerSession` | `claimKey`: the receipt (correlates the journal stream). `runId`: the supervisor's `dispatch_intent`. `packageRef` is the **supplied-context identity**, either: the exact `suppliedSha256` of the task text **plus** the separately captured system, fast and deep instruction strings (or their digests) and the content and prerequisite digests (023 §2.3, 025); or a durable, immutable stored-package reference. `packageToken` alone is correlation only, never identity. `workerSession`: a transport-level identity, named in `dispatch_intent` **before** sending |
| **ReviewKey** | `rootId, nodeId, attemptId, attemptEpoch, artifactRef` + `lead` + `leadSession` | `artifactRef`: PlanStore Done. `lead`: the `review_requested` record. `leadSession`: bound durably as described below |
| **MessageKey** | `notify:<claimKey>:<epoch>:<purpose>:<n>` | 026 §4 dedup key |
| **ActId** | `(ExecutionKey or ReviewKey, actKind, actSeq)` | `actSeq` is chosen by the actor and is strictly increasing per key |

**Frozen wire shapes (msg 1139; P, revision 4).** These are frozen as **fixture constants for the bounded E2c experiment only** (msg 1145). The proposed `hkw1` format, the `packageRef` kinds and the §4.1 values are **not** accepted production identity, auth or default contracts; each would need its own review and GO.
- **`executorRef` ↔ `workerSession` consistency:** a dispatchable claim **must** carry an `executorRef`, and `workerSession` is **byte-equal** to the current attempt's `executorRef` as PlanStore holds it (receipt and node state). PlanStore already enforces 1–256 printable ASCII with no spaces (V `PlanRules.cs:462`), binds it at start/reopen, rejects a mismatching `executorRef` on finish or release (V `PlanRules.cs:365–366`), and keeps it on Done (V `PlanRules.cs:369`). The proposed value format is `hkw1:<principal>:<sessionId>`, with `principal` matching `[a-z0-9][a-z0-9-]{0,63}` and `sessionId` a lowercase 36-character UUID (I: a bridge session id is a natural source, OQ2). A claim with a null or non-conforming `executorRef` is not dispatchable; the supervisor records nothing and queues one operator entry.
- **`packageRef`:** canonical JSON (RFC 8785 JCS), compared by **exact byte equality**, in exactly one of two forms; an unknown `kind`, a missing or extra field, or a non-conforming value is **malformed**:
  - `{"kind":"supplied.v1","suppliedSha256":H,"instructions":{"system":H,"fast":H,"deep":H},"contentRevision":N,"contentDigest":H,"prereqDigest":D}`
  - `{"kind":"stored.v1","storeRef":S,"sha256":H}`
  - `H` is 64 lowercase hex characters (SHA-256). `N` is the attempt's pinned content revision (an integer, §4.1 limits). `D` is the attempt's pinned prerequisite digest **exactly as the PlanStore claim receipt returns it**. `S` is 1–256 printable ASCII with no spaces and must name an immutable stored package. Instruction strings are carried only as digests. `packageToken` never appears in `packageRef`.

**4.1 Frozen integer limits (msg 1139; P; fixture constants only, msg 1145).** An integer on the wire is a JSON number token with no fraction and no exponent, within the range stated below. A JSON boolean, a string, `null`, a fractional value (including `1.0`) or an exponent form (`1e3`) is **malformed**, never coerced. A PlanStore value above `2^53 − 1` cannot be carried; the key or field fails closed (`unknown`).

| Limit | Value |
|---|---|
| `actSeq`, `checkpointId` | `1 … 2^53 − 1`, strictly increasing per key |
| `attemptEpoch` (in any AttemptKey, ExecutionKey or ReviewKey) | `1 … 2^53 − 1`. A started attempt always has epoch ≥ 1: the column defaults to 0 for a never-started node and start increments it (V `PlanStoreSchema.cs:71`, `PlanRules.cs:358`). Epoch 0 in a key is malformed |
| `contentRevision` (including the `packageRef` pin) | `1 … 2^53 − 1` (V `PlanStoreSchema.cs:67`, `content_revision >= 1`) |
| `stateRevision` / `snapshotRevision` | `0 … 2^53 − 1` (V `PlanStoreSchema.cs:68`, `state_revision >= 0`) |
| event `seq`; `eventsAfter` cursor | `seq`: `1 … 2^53 − 1` (V `PlanStoreSchema.cs:104`, `seq >= 1`). `eventsAfter`: `0 … 2^53 − 1`, where 0 means from the start |
| **G**: novel progress checkpoints per key, and retained accepted evidence digests per key | 64 |
| **V**: route B observations per key | 16 |
| **K**: notification intents per key per purpose | 3 |
| Operator-queue entries | 1 per key per reason |
| Counters | unsigned 32-bit, saturating at `2^32 − 1` with a per-counter `saturated: true` flag |
| Act payload | ≤ 16 KiB canonical JSON; strings ≤ 1,024 characters unless stated otherwise |
| Deadline windows (ACK, first progress, progress; worker and review), seconds | each `60 … 86,400`; proposed defaults 900 (ACK), 1,800 (first progress), 1,800 (progress) |
| C-B request | 1–32 explicit keys; evidence page `limit` 1–200 (default 50); response body ≤ 1 MiB |

**Session binding (msg 1105 §3):**
- **Worker:** `workerSession` is bound **before** dispatch, in the durable `dispatch_intent`, and must equal the attempt's `executorRef` (above). A worker ACK from any other session is `foreign_session`.
- **Lead:** one of two durable, authorized routes binds `leadSession`. In neither is the session merely assumed.
  - (a) **Pre-binding:** `review_requested` (or a later operator `review_assigned` act) names the lead **and** the lead's session.
  - (b) **Atomic bind+ACK:** if no session is bound yet, the first ACK that validates from the **named lead** binds its session and records the ACK in the **same** append. It is refused if a session is already bound.
- **Changing a binding:** a new `leadSession` for the same review is a distinct `review_rebind` act. It needs the old session's explicit release or an operator act. A worker session **cannot** be rebound inside an attempt, because `executorRef` is fixed for the attempt in PlanStore. A new worker session needs a PlanStore release or reopen, which is a new attempt or epoch (a supersession, §5). Revision 3's `worker_rebind` is withdrawn.
- **Deadlines:** a binding, rebind or new session **never re-anchors** a deadline. Deadlines stay anchored at the original `dispatch_intent` or `review_requested` and at the last *novel* progress (§7), so a fresh session cannot buy time.

## 5. States and what each means (P)

**Per execution (AttemptKey + runId):**

```
allocated ──dispatch_intent──▶ dispatched ──(transport)──▶ delivered ──(transport)──▶ consumed
   │ (PlanStore claim)          (journal intent)           (message id)              (read flag; advisory)
   │
   └─▶ acknowledged ──▶ progress* ──▶ result_captured ──▶ finish_intent ──▶ finish_outcome
       (worker_ack act)  (worker_progress acts)   (existing E2a / 026 records)
```

| Term | Definition | Evidence | Authorizes |
|---|---|---|---|
| **allocated** | PlanStore receipt plus `attempt_started` | PlanStore | Nothing about execution |
| **dispatched** | The supervisor recorded `dispatch_intent` (an agent session) or `launch_intent` (a process), **before** sending. This proves the **intent** only: whether the send happened is known only from `dispatch_outcome`, and an intent without an outcome is unknown (026 §2) | Journal intent | The send |
| **delivered** | The transport accepted the message and returned a message id | `dispatch_outcome{delivered, messageId}` | Nothing; delivery ≠ action |
| **consumed** | The transport reports the recipient read it | Advisory `consumed_observed` (bounded; one per message) | Nothing; reading ≠ starting |
| **acknowledged** | The bound worker session sent `worker_ack{ExecutionKey, actSeq=1}`, and it **validated** (§6). This is an **attributed self-report of take-up**. It is **not** proof that a process is alive or that any effect has started | `worker_ack` record (effective only; §6) | The "taken up (self-reported)" status; ends the ACK deadline and starts the **first-progress** deadline, anchored at this ACK (§7) |
| **progress** | `worker_progress{ExecutionKey, actSeq, checkpointId, evidenceDigest}` with a **new** checkpoint id **and** new evidence content (026 §7 rule, E2a `evidence_digest`). It is also an attributed self-report | `worker_progress` record (novel, effective acts only; §6 step 5) | The first one ends the first-progress deadline; each novel checkpoint then re-anchors the progress deadline at itself |

**How a worker stream ends (msg 1139):**
- **Finished (normal):** PlanStore moves the node `InProgress → Done` for **exactly** this `attemptId` and `attemptEpoch`. PlanStore keeps `executorRef` and the pins on Done (V `PlanRules.cs:368–369`). The stream is `finished`, **not** superseded, **whoever** recorded the transition: the worker, the supervisor or another actor finishing that same attempt. Its ACK, progress and finish evidence stay bound to that completed AttemptKey and `artifactRef`, and the review stream for that exact ReviewKey starts. Later worker acts for the stream are counter only (`stale`) and change nothing that was established.
- **Superseded (distinct cases):** `release` (`InProgress → Todo`, which clears the attempt), `cancel`, `reopen` (a new epoch) and any other epoch or attempt replacement. Each is recorded as `superseded{reason: released | cancelled | reopened | epoch_replaced}`. Its evidence is kept as **historical** (§8 C-B) and is never current for the new attempt.

**Per review (ReviewKey):** `requested` (`review_requested` after an authoritatively confirmed Done for *this* stream, C8), then `delivered`/`consumed` (transport, advisory), then `acknowledged` (`review_acknowledged`, lead act, validated and effective; starts the first-progress deadline, §7), then `progress*` (`review_progress`, new checkpoint + new evidence; the first one ends the first-progress deadline), then **ended** by a PlanStore decision on exactly this ReviewKey. It is **moot** when the current PlanStore facts no longer match the exact ReviewKey (reopened, cancelled, other epoch or artifact). Revision 3 also listed "decided" under moot; a decision on the exact key **ends** the review instead. This is the same rule as E2b-a's `current_review` (`e1/recovery.py`).

## 6. Validation of an inbound act (P)

Each inbound ACK or progress act is processed in the order below. **Atomicity and coherence** (msg 1105 §5) follow one of two routes, and the doc promises nothing in between:
- **(A) One reviewed transaction:** if the journal lives in the PlanStore database (OQ1), the supervisor reads the current PlanStore rows it validates against **and** appends the act in **one** transaction under a reviewed lock order (028 §6, extended to the PlanStore rows read). That needs its own contract and GO.
- **(B) Audit-only observation** (msg 1112 §2): otherwise, the act is recorded as a bounded **observation** carrying the PlanStore facts that an *independent* read returned (`stateRevision`, attempt and epoch, the last event `seq`, and the review class with its source).
  - Its `currentness_at_append` is **`unverified`**: independent reads cannot prove whether the facts were still current when the append happened.
  - Later reconciliation can say only **`superseded_as_of_read`**: the facts that read returned had been superseded by the time of a later read. It says nothing about when the append happened relative to the change.
  - An unverified observation **never** advances a consumer's *current* acknowledged or progress status, never resets or starts a deadline, and never counts as the ACK.
  - It becomes effective only through route A validation or an explicit operator act (`operator_resolution{decision: "confirm_act", reconciliationRef}`), and even then deadlines keep their original anchors: the confirmed act anchors at its original observation `at`, never at the `confirm_act` (§7).
  - Two route B reads that return matching revisions do not upgrade an observation; they are still independent reads (§2.8).
- **Never:** a C-A read followed by a separate journal read or write is two reads, not one snapshot, and is never presented as one.

Steps:
1. **Shape:** a full key, typed bounded payload (E2a `_check_payload`), and a known act kind. Otherwise **malformed** (counter only).
2. **Current:** the identity matches current PlanStore facts. Under route A this is checked inside the append transaction. Under route B it is checked against an independent read, and the result is `unverified` (route B rules above).
   - For a worker act: the node is `in_progress` for exactly `attemptId` and `attemptEpoch`, and the journal's `dispatch_intent` has the same `claimKey`, `runId` and **`packageRef`** (§4).
   - For a review act: the node is Done for exactly the ReviewKey's attempt, epoch and artifact, and the review class is `candidate`. `decided_unverified` and `operator_classification` go to an operator.
   - Otherwise the act is **stale** (counter only).
3. **Session:** the act's session equals the **bound** session (§4), or this is the single allowed atomic bind+ACK for a named lead with no session bound yet. Otherwise **foreign_session**: counter only, plus at most one operator-queue entry per key.
4. **Identity of the act** (msg 1105 §1): look up the ActId.
   - **Duplicate:** the same ActId with an **exactly identical** held payload (canonical JSON, including `checkpointId` and `evidenceDigest`). It is an idempotent no-op that returns the original record id.
   - **Conflict:** the same ActId with **any** different payload. Counter only (`conflict`) plus at most one operator-queue entry per key. It never replaces the original and never changes a deadline.
   - **Out of order:** `actSeq` lower than the last accepted one (counter only). Progress also requires a prior accepted ACK.
5. **Novelty** (msg 1105 §2): progress needs **new** evidence content **and** a strictly greater `checkpointId`. Anything else is **counter only** (`not_novel`): no record, no deadline reset.
   - **Bookkeeping per key** (bounded, msg 1112 §1): the last accepted `actSeq` and `checkpointId`, and **every** accepted novel `evidenceDigest`, at most G of them. None is evicted while the key is current, so a replayed old digest is always recognized and is `not_novel`.
   - **Unknown novelty fails closed:** if the bookkeeping is unavailable, incomplete or corrupt, the act is counter only (`novelty_unknown`) plus one operator-queue entry. It is **never** treated as novel.
   - **Cap:** after G novel checkpoints, further acts are counter only (`progress_overflow`), and the key gets a single operator-queue entry. **Reaching the cap preserves what is established** (msg 1139): the accepted ACK, the G accepted checkpoints, the current status and the current deadline (anchored at the last accepted novel progress) all stay valid. The cap never turns status into `unknown`.
   - **Cap vs failed proof:** the cap is a bound on *valid* bookkeeping. Bookkeeping that is incomplete, corrupt or unreadable is a different case. It fails closed (`novelty_unknown` for new acts, and `unknown` for the C-B fields whose required proof it is) until it is repaired or an operator resolves it.
6. **Append:** one idempotent journal append (028 §3), keyed by ActId. Under route A it is an effective act. Under route B it is an audit-only observation with its read facts and `currentness_at_append: unverified`. Its outcome is confirmed per the E2b-a `confirm()` rules: only `committed` authorizes anything, and `not_observed` or `compacted` authorize nothing (029).

**Counters** (malformed, stale, foreign_session, conflict, out_of_order, not_novel, novelty_unknown, progress_overflow, duplicate) are per-stream, fixed-width (§4.1) and **saturating**: they stop at their maximum instead of wrapping, and saturation is itself reported. Counter saturation is diagnostic only. It never changes status, deadlines or accepted records (msg 1139).

None of these steps writes PlanStore. A duplicate, conflicting, stale, foreign or non-novel act **never** advances state or resets a deadline.

## 7. Deadlines, restart and bounds (P)

- **Anchors:** every deadline is computed from the **durable `at` of the anchoring record**. Each stream has exactly one live deadline at a time, in this sequence (msg 1125):

  | Phase | Worker stream | Review stream | Ends when |
  |---|---|---|---|
  | ACK | anchored at `dispatch_intent` | anchored at `review_requested` | an **effective** ACK is recorded |
  | **First progress** | anchored at the effective `worker_ack` | anchored at the effective `review_acknowledged` | the first novel, effective progress act |
  | Progress | anchored at the last novel, effective `worker_progress` | anchored at the last novel, effective `review_progress` | finish (worker) or decision / moot (review) |

  - Any phase also ends when its stream ends: `finished` or `superseded` for a worker, a decision or moot for a review (§5).
  - Only **effective** acts (route A, or route B confirmed by operator `confirm_act`) end one phase and anchor the next. Transport ACKs, process launches, deliveries, reads and unverified observations anchor nothing (§2.3, §2.9).
  - **`confirm_act` never re-anchors** (msg 1125). When an operator confirms a route B observation, the confirmed act anchors the next phase at the act's **original observation `at`**, never at the time of the `confirm_act`. The `confirm_act` record's own `at` is audit only. If the deadline computed from the original anchor has already passed, the key is reported overdue at once (advisory only); confirmation never buys time.
  - A deadline that has expired without its ending act stays overdue until that act arrives. A later act ends the overdue phase and anchors the next phase at its own original `at`.
  - `at` is the database clock sampled inside the append transaction (026 OQ2, proposed answer). For a route B observation it is the observation's append `at`, which is what `confirm_act` preserves.
  - A restart rehydrates from the journal plus current PlanStore facts (E2a `ReviewWorkflow.rehydrate`). It **never** re-anchors to "now".
  - A session bind, rebind or new session **never** re-anchors (§4).
- **Restart keeps pending reviews:** `review_requested` is durable, and candidacy is re-derived from PlanStore on every scan. A review survives any number of restarts until it is decided or moot.
- **On expiry, advisory only** (msg 1105 §7): the scan reports the overdue key and an *intended* notification to the **same** worker or lead (bounded by K, counting every `notify_intent`, 026 §4), then a bounded operator-queue entry. Expiry **never** triggers an automatic action: no reassignment, release, relaunch, decision or state change.
- **Per-key caps:**
  - ACK: one per ExecutionKey (a re-ACK is a duplicate or a conflict);
  - progress: G novel checkpoints, then counter-only overflow, with the established status and deadline preserved (§6 step 5);
  - accepted evidence digests: at most G per key, all retained while the key is current;
  - route B observations: at most V per key (counter beyond it);
  - notifications: K per purpose;
  - consumed observations: one per message;
  - operator-queue entries: at most one per key per reason.
- **Global caps:** 028 §6 (singleton accounting, worst-case reservations, eviction of resolved data only) and the operator-queue rules of 029 (Q/Y, explicit overflow, refresh then reconcile).

## 8. Proposed contracts (shape only; each needs its own GO)

**C-A. PlanStore read-only snapshot (PlanStore-owned).**
- `GET /api/plan-contract/v1/nodes/{nodeId}/snapshot?eventsAfter=&limit=` returns the node state, a bounded page of that node's events, the claim receipt(s) for the current attempt, `snapshotRevision`, and `complete`.
- One REPEATABLE READ READ ONLY transaction **over PlanStore rows only**, loopback-gated like the rest of the contract. It replaces direct table access for PlanStore facts (026 OQ6, 028 OQ5).
- It makes **no** claim of coherence with the journal. A C-A read followed by any journal read or write is two reads. Effective acts need route A of §6 (same database, one reviewed transaction). Otherwise acts are audit-only observations (route B).

**C-B. Supervisor evidence read (supervisor-owned, read-only).**
- **Request identity (msg 1145).** A request names one of the following, and **every response echoes the complete resolved identity**: the full ExecutionKey (with `packageRef` in its JCS form and `workerSession`), or the full ReviewKey (with `lead` and `leadSession`).
  - **Full key:** the caller supplies the full ExecutionKey or ReviewKey. An ExecutionKey must match its durable `dispatch_intent` **byte for byte**, field by field; a worker session is fixed for the attempt (§4). A ReviewKey is matched against the **current resolved binding** (below), not against `review_requested` alone. Any difference refuses the request with `identity_mismatch`, naming the fields that differ.
  - **Review binding chain (msgs 1148, 1152).** A ReviewKey's `lead` and `leadSession` resolve from the durable, authorized chain for that exact ReviewKey, **ordered by predecessor linkage and the journal's durable append sequence, never by `at`**: `review_requested` (which may name the lead and session) → an optional operator `review_assigned` → or the single atomic bind+ACK → zero or more `review_rebind` acts (each gated on the old session's release or an operator act, §4).
    - **Linkage:** every binding link after `review_requested` carries `predecessorBindingId`, the journal record id of the exact binding it replaces. `review_requested` is the root, with no predecessor.
    - **Appending a link** happens inside the reviewed transaction (route A, §6). The append succeeds only if `predecessorBindingId` equals the **current binding at that moment**, a compare-and-set. Otherwise the link is refused as `stale_binding` and nothing is appended.
    - **Resolving a chain:** start at the root and follow successor links. Each committed link (§6 step 6) must have **exactly one** committed successor or none, and the predecessor's journal append sequence must be lower than its successor's. The **current** binding is the link with no successor.
    - **Timestamps play no part in ordering.** `at` values may tie or regress (clock steps, equal samples). They are **deadline anchors only** (§7) and never decide which binding is current.
    - Earlier bindings are returned in `historical` and are never current.
    - A full ReviewKey that names an **earlier** (superseded) `leadSession` is refused as `stale_binding`, with the current binding's `at` given but no deadline effect.
    - **Gaps and forks fail closed.** A link whose predecessor is missing or uncommitted is a gap. Two committed successors of one link is a fork. So is a successor whose append sequence is not greater than its predecessor's. Each is refused as `ambiguous_binding`, with one operator-queue entry; it never picks one. An unreadable or corrupt chain makes the identity unresolved, and the review fields are `unknown`.
    - A ReviewKey with no bound session yet resolves with `leadSession: null`, and that null is echoed. A caller that supplies a session for it gets `identity_mismatch`.
    - Binding, assignment and rebind **never change a deadline anchor** (§4, §7). They change only the identity that is echoed.
  - **Lookup by attempt and run:** the caller supplies the full AttemptKey (all four fields) plus `runId`. The supervisor resolves the rest through the **durable `dispatch_intent` mapping**. That mapping is unique by construction: one `dispatch_intent` per `runId`, and `runId` is never reused for another AttemptKey. Exactly one match resolves. No match is refused as `unknown_key`. More than one match is a corrupted mapping, refused as `ambiguous_key` with one operator-queue entry. The refusal never picks one.
  - **AttemptKey alone:** refused as `ambiguous_key` when the attempt has more than one run. With exactly one run it resolves, and the response still echoes the full key. With no run it is `unknown_key`. Either way it is never matched loosely.
  - **Optional fields:** if the caller supplies any of `claimKey`, `packageRef` or `workerSession` with a lookup, each must equal the resolved value exactly, or the request is refused with `identity_mismatch`.
  - **Review lookup:** the full AttemptKey plus `artifactRef` resolves through `review_requested` **and the binding chain above**. It echoes the current full ReviewKey, including the current `lead` and `leadSession`. A partial or loose key is always rejected.
- Per resolved key it returns the derived execution/review fields (§5 terms), the current deadline phase (§7) with its anchoring record and `at`, counters, and the queue view.
- **Bounded per-key reads only:** one key per request (or a bounded, explicitly listed set), with a bounded page of evidence records and a cursor. No unbounded listing, no "all streams" query.
- **Two separate sections per key** (msg 1139):
  - **`current`**: present only when it is derived from **one combined snapshot**, meaning route A: a single reviewed transaction that reads both the PlanStore rows and the journal. Otherwise `current` is `unknown`. It is never assembled from independent reads, even when their revisions match (§2.8).
  - **`historical`**: as-of evidence such as route B observations, superseded streams (§5), finished-stream records and transport facts. Each item carries its own read basis and `as_of`, and is never presented as current.
- Every response carries a **basis**: the route A transaction id, or the independent reads it used, each with its revision.
- **Proof completeness is per derived field and separate from paging** (msg 1145). There is no blanket `complete` flag.
  - Each derived field (`ack`, `deadlinePhase` with its anchor, `progress`, `review`) carries `proof: complete | missing | corrupt | unverified | not_observed`.
  - A field's **required proof** is fixed: the PlanStore facts for the exact key from the combined snapshot, the anchoring record and its committed outcome (§6 step 6), and, for `progress`, the per-key bookkeeping (§6 step 5).
  - A field is `unknown` only when its own required proof is not `complete`. Other fields are unaffected.
  - **Retained verified anchors are required proof and are never evicted while the key is current** (028 §6 evicts resolved data only). These are the effective ACK, the last accepted novel progress and the G retained digests. A cap, counter saturation (§6) or the compaction of an unrelated or superseded record therefore never erases them.
  - **Paging is separate:** the `historical` page has `nextCursor` and `truncated`. Truncated or compacted optional history affects only the page and never a derived field.
  - A basis older than current PlanStore facts makes the affected fields `unknown`, and the response says which fields.
- It is derived per request from the journal plus current PlanStore facts, and **never** stored as task state. Route B observations are shown separately as `unverified` and never as the current acknowledged or progress status. It never implies one snapshot, even when its revisions match a separate C-A read (§2.8).
- This is what ChatAgent would read to replace `executionAcknowledged: "unknown"` (§9).

**C-C. Act intake (supervisor-owned).**
- Workers and leads submit `worker_ack`, `worker_progress`, `review_acknowledged` and `review_progress` acts, carrying the §4 keys (including `claimKey` and `packageRef`), to the supervisor. The supervisor validates them and appends: an effective act under route A, an audit-only observation under route B (§6).
- The transport is the bridge for agents. An act arriving by bridge is still just a message until §6 accepts it.
- **No PlanStore writes.**

**C-D. Vocabulary additions to the journal (supervisor-owned):**
- `dispatch_intent` / `dispatch_outcome{delivered|failed|unknown, messageId}`;
- `consumed_observed`;
- `worker_ack`, `worker_progress`;
- `review_assigned` (operator pre-binding of the lead session) and `review_rebind` (operator- or release-gated); neither re-anchors a deadline. There is no `worker_rebind` (§4);
- stream end records: `finished` (a normal matching finish) and `superseded{reason}` (§5);
- route B observation records (the facts of the independent read, `currentness_at_append: unverified`) and the later `superseded_as_of_read` audit classification; operator `confirm_act`;
- per-key bounded bookkeeping (last `actSeq` and `checkpointId`, every accepted novel evidence digest up to G);
- saturating per-stream counters (malformed, stale, foreign_session, conflict, out_of_order, not_novel, novelty_unknown, progress_overflow, duplicate).

All extend the E2a/E2b-a rules unchanged: reservations, required outcomes, idempotent ids, fencing and corruption retention.

## 9. Reconciling ChatAgent C1a (`98c3d12`)

| C1a today | Under this boundary |
|---|---|
| `in_progress` from PlanStore `work`; `executionAcknowledged: "unknown"` | Still correct without C-B. With C-B, `in_progress` refines to `allocated` / `dispatch_intended` / `delivered` / `taken_up_self_reported` / `progress_self_reported` / `ack_overdue` / `first_progress_overdue` / `progress_overdue` / `finished`. The labels say "self-reported" because an ACK is not proof of a live process (§2.7), and "overdue" is advisory only. ChatAgent must keep a field `unknown` whenever C-B is unavailable, that field's `proof` is not `complete`, or it has only route B `unverified` observations for it. A truncated `historical` page never makes a field unknown (which may be displayed separately as unverified, never as the current status) |
| `review_pending` = Done + `none` | Matches the PlanStore candidate. With C-B: `review_requested` (lead named), `review_acknowledged`, `review_progressing`, `review_overdue` and `review_unassigned` (a candidate with no request yet). `decided_unverified` and `operator_classification` stay operator states |
| `stale` = Done + `accepted` + `upstreamChanged` | Consistent with PlanStore `stale`. The boundary adds nothing here |
| Delivery is "never an acknowledgement" | Same rule (§5): `delivered` and `consumed` authorize nothing |
| The read-only, loopback, fail-closed bounds | C-B should match: the same loopback literal, strict schema, `contractVersion`, bounded body, and per-key results rather than unbounded listing |

**ChatAgent must not:**
- infer ACK from claim, delivery or reading;
- treat a C-B absence as "not started";
- write any act on a worker's or lead's behalf (C-C acts come from the worker or lead session itself);
- persist anything (msg 1124): no cache, store or journal of C-B, bridge or PlanStore results beyond rendering one response. ChatAgent stays a stateless, read-only consumer with **no writes** to PlanStore, the journal or the bridge;
- treat a bridge transport ACK, session, lease, work claim or harness `launched` / `completed` as execution or review evidence (§1.5, §2.3);
- let an evidence failure change plan status: if C-B fails, ChatAgent still shows the C1a PlanStore leaf state, with evidence fields `unknown` (§2.10).

## 10. Not included

- Any implementation, test, schema, migration, service, endpoint, CLI or bridge change.
- Production journal storage selection (028 OQ1). A production coherent-read implementation (C-A is shape only).
- A wake mechanism. Auth for workers, leads or operators. Process launch and owned-group cleanup (E3). Leases and heartbeats (3b2).

## 11. Open questions

1. **Storage** (028 OQ1): may the journal live in the PlanStore database? Only then is route A (one reviewed transaction covering the PlanStore rows read and the journal append) possible; C-A plus a separate journal read never is.
2. **Session identity:** what is the authoritative `workerSession` and `leadSession`: a bridge agent name plus a session uuid minted at dispatch, or something the agent runtime provides? How is a rebind authorized before auth exists?
3. **Active watcher** (026 OQ5): who calls `scan(now)` in practice: an operator-run CLI, a scheduled task, or a supervisor loop? None is a wake. Each needs its own GO.
4. **Default bounds:** proposed and frozen for review in §4.1 (revision 4). Open only as to whether the proposed defaults are right.
5. **Lead ACK semantics:** must a lead's ACK name the evidence they will use (for example, a test plan), or only the ReviewKey?
6. **Supported wake (a required capability gap, msg 1105 §7):** what supported mechanism will reliably bring a responsible session (or an operator) to act on an overdue obligation? It must be designed and accepted separately. Until then the boundary relies on an active watcher (Q3) and advisory expiry. Delivery is never treated as resumption.
7. **Transaction route** (§6): is route A (journal in the PlanStore database, one reviewed transaction) wanted, or is route B (audit-only observations, effective only by operator act) the production contract?
8. **Bookkeeping bounds:** proposed in §4.1 (G = 64, V = 16, 32-bit saturating counters). Open only as to whether those values are right.

## 12. Smallest next step: proposed fixture-only E2c scope (proposal only; no tests or code GO)

A **test-only E2c** on the existing E2b-a fixture (`scripts/local/supervisor_e1/`, disposable `hekate_plan_e1_*` databases). Nothing is written until codex-hekate gives a GO.

**Fixture changes:** add the C-D vocabulary (§8) to the fixture journal schema and adapter, with the §4 keys, §4.1 limits and the `packageRef` / `executorRef` wire shapes. No production schema, migration, endpoint or service.

**Cases to prove (each one asserts status, the deadline phase and anchor, and the counters):**
1. **Shapes and limits** (fixture constants, §4):
   - partial keys are malformed;
   - non-JCS or extra/missing `packageRef` fields are malformed, and so is an unknown `kind`;
   - out-of-range integers, an oversize payload, and a null or non-conforming `executorRef` (not dispatchable) are malformed;
   - integer boundaries (msg 1145): `attemptEpoch` 0 is rejected and 1 accepted; `contentRevision` 0 is rejected and 1 accepted; `stateRevision` 0 and `eventsAfter` 0 are accepted; `actSeq` and `checkpointId` 0 are rejected; `2^53 − 1` is accepted and `2^53` rejected; `true`, `false`, `"1"`, `null`, `1.0`, `1.5` and `1e3` are rejected wherever an integer is expected;
   - a `workerSession` that is not byte-equal to `executorRef` is `foreign_session`.
2. **§6 outcomes:** stale, foreign-session, exact-duplicate (returns the original id) vs **conflict**, out-of-order, not-novel, a replayed old digest staying `not_novel`, and `novelty_unknown` failing closed. Each is counter only, with no deadline effect.
3. **Deadline phases (§7):**
   - ACK anchored at `dispatch_intent`;
   - **first progress anchored at the effective ACK**;
   - progress anchored at the last novel progress;
   - the same for review (`review_requested` → `review_acknowledged` → `review_progress`);
   - one live deadline per stream; an overdue phase that is later satisfied anchors the next phase at the act's own `at`.
4. **`confirm_act` without re-anchoring:**
   - a route B observation confirmed later anchors the next phase at its **original observation `at`**;
   - the `confirm_act` time is ignored, and an already-past deadline is reported overdue immediately.
5. **Route B audit-only:** no status advance and no deadline effect; `superseded_as_of_read`; two reads with matching revisions do **not** produce a `current` section (§2.8, C-B).
6. **Restart:** ACK, first-progress and progress deadlines survive a restart anchored at durable `at`, never at "now"; pending reviews survive.
7. **Stream ends (§5):**
   - a normal matching `InProgress → Done` by the worker **or by another actor** is `finished`, keeps the evidence and starts review, and is never `superseded`;
   - release, cancel, reopen and epoch replacement each give `superseded{reason}`, and the evidence moves to `historical`;
   - review ends on a decision on the exact key, and is moot otherwise.
8. **Saturation:**
   - at the G cap, the established status, accepted checkpoints and current deadline are preserved and further acts are `progress_overflow`;
   - counter saturation at `2^32 − 1` is flagged and has no other effect;
   - corrupt or incomplete bookkeeping fails closed separately (`novelty_unknown`, and C-B `progress` `unknown` only).
9. **Bindings:** atomic lead bind+ACK and its refusal when a session is already bound; `review_rebind` never re-anchors; there is no worker rebind inside an attempt.
10. **C-B shape (fixture function, not an endpoint):**
    - exact per-key requests and bounded pages;
    - `current` only from the combined route A snapshot, and `historical` separately;
    - `basis` and per-field `proof` present;
    - **identity** (msg 1145): two runs of one attempt (AttemptKey alone gives `ambiguous_key`; AttemptKey + `runId` resolves the right one with the full key echoed); two `packageRef`s across those runs (a supplied mismatching `packageRef` gives `identity_mismatch`); a `workerSession` mismatch gives `identity_mismatch`; an injected duplicate `runId` mapping gives `ambiguous_key` plus an operator entry and is never resolved; an unknown `runId` gives `unknown_key`; a ReviewKey lookup by AttemptKey + `artifactRef`; **review binding chain** (msg 1148):
      - before any binding, the lookup echoes `leadSession: null`;
      - after an atomic bind+ACK, the lookup and the full key with the bound session resolve, and the full key with no or another session gives `identity_mismatch`;
      - after an operator `review_assigned`, the lookup echoes the assigned session;
      - after a `review_rebind`, the new session resolves, the old session gives `stale_binding`, and the old binding appears under `historical`;
      - an injected forked chain gives `ambiguous_binding` plus an operator entry; a corrupt chain gives review fields `unknown`;
      - in every case, the deadline phase and anchor are unchanged by bind, assign or rebind;
      - **ordering** (msg 1152): two links with an **equal** `at`, and a successor whose `at` **regresses** below its predecessor's, both resolve by predecessor linkage and append sequence, giving the correct current binding. Both deadline anchors stay at their original `at` values;
      - a rebind whose `predecessorBindingId` is not the current binding is refused as `stale_binding` (compare-and-set) with no append;
      - a link that references a missing predecessor (a gap), and a successor whose append sequence is not greater than its predecessor's, each give `ambiguous_binding`;
    - **per-field completeness** (msg 1145): a corrupt progress bookkeeping row makes `progress` `unknown` while `ack` and its anchor stay `complete`; a missing ACK outcome makes `ack` and later phases `unknown`; compacting an unrelated superseded run's records changes no field of the current run; a truncated `historical` page (`truncated: true`, `nextCursor`) leaves every field unchanged; at the G cap and with saturated counters, the retained anchors stay `complete`;
    - with the journal unavailable, PlanStore leaf state is still returned unchanged and evidence is `unknown` (§2.10).
11. **Transport and launch are not acts:** a fixture that records a bridge-style transport ACK, `launched{pid}` and `completed{artifact_ref}` shows none of them changes the Hekate status or anchors a deadline.
    - **Reuse, don't duplicate** (msg 1124): bridge behaviour itself (ACK idempotency, work-claim transitions, the harness worker) is already covered by the bridge's own tests at `7a33328`. E2c only records their outputs as inputs and does not re-test or reimplement them.

**Not in E2c:** no production change, no PlanStore schema change, no bridge change, no live bridge or harness worker, no wake, no ChatAgent change, no auth. It needs its own GO.

## 13. Revision 2 changes (codex-hekate msg 1105)

1. **Duplicates vs conflicts:** the same ActId is a duplicate only when the held payload is **exactly** identical. Any different payload is a **conflict**: counter only, with no deadline change (§6 step 4).
2. **Novelty is counter-only:** a non-novel act appends nothing. Bounded bookkeeping (last `actSeq` and `checkpointId`, an H-digest ring, the G cap with explicit overflow) and saturating counters replace "record without reset" (§6 step 5, §7). *(The H-digest ring is superseded in revision 3, §14.)*
3. **Lead session binding:** durable pre-binding (`review_requested` or operator `review_assigned`), or an atomic bind+ACK from the named lead. No binding re-anchors a deadline (§4, §7).
4. **Attribution:** `dispatch_intent` proves intent only. A worker ACK or progress act is an attributed self-report, not proof of a live process or a started effect (§2.7, §5, §9 labels).
5. **No snapshot promise from independent reads:** route A (one reviewed transaction in the same database) or route B (as-of facts plus `stale_at_append` audit). C-A covers PlanStore rows only (§6, §8). *(Route B is made audit-only and `stale_at_append` withdrawn in revision 3, §14.)*
6. **Supplied-context identity:** the ExecutionKey binds `claimKey` and a `packageRef` (`suppliedSha256` plus the separately captured instructions and digests, or an immutable stored-package reference). `packageToken` alone is correlation only (§4).
7. **Expiry is advisory**, with no automatic action. A supported wake is a **required capability gap**, specified separately (§1.3, §2.6, §7, OQ6).

## 14. Revision 3 changes (codex-hekate msg 1112)

1. **No unknown-as-novel:** every accepted novel evidence digest is retained up to G, with no eviction while the key is current, so a replayed old digest is always `not_novel`. If novelty cannot be determined, the act fails closed (`novelty_unknown`, counter plus operator) (§6 step 5, §7). This replaces revision 2's H-digest ring.
2. **Route B is audit-only:**
   - `currentness_at_append` is `unverified`;
   - later reconciliation says only `superseded_as_of_read`. Revision 2's `stale_at_append` is withdrawn, because independent reads cannot establish append timing;
   - unverified observations never advance a consumer's current status or reset or start a deadline;
   - only route A or an explicit operator `confirm_act` makes an act effective, with original anchors kept (§3, §6, §8, §9, OQ7).

## 15. Revision 4 changes (consumer verdict msg 1124, relayed in msgs 1125 and 1138; lead guidance msgs 1139 and 1141)

1. **First-progress deadline:** there is one live deadline per stream, in phases: ACK (anchored at `dispatch_intent` or `review_requested`), then **first progress (anchored at the effective `worker_ack` or `review_acknowledged`)**, then progress (anchored at the last novel progress). This resolves the gap between revision 3's §5 and §7 (§5, §7).
2. **`confirm_act` never re-anchors:** a confirmed route B act anchors at its original observation `at`, never at the confirmation time (§6, §7).
3. **Bridge source facts** updated and pinned to committed agent-bridge `7a33328`: explicit ACK, sessions, leases, durable work claims, and the opt-in harness worker. Uncommitted `launcher.py` / `cli.py` work is listed separately as not accepted. E2c reuses the bridge's own tests (§1.3, §1.5).
4. **Transport ACKs and process launches are not acts; no existing IDE wake is proven** (§1.3, §2.3).
5. **Matching revisions across independent reads are not coherent evidence** (§2.8, §6, §8).
6. **Only current, verified evidence advances current status; the plan survives evidence failure** (§2.9, §2.10, §9).
7. **C-B** returns exact per-key, bounded reads with `basis` and completeness (revision 5 replaces the blanket `complete` flag with per-field `proof`, §16). It has a `current` section only from one combined route A snapshot, and a separate `historical` / as-of section (§8).
8. **A normal matching finish is `finished`, not superseded,** whoever records it (PlanStore keeps `executorRef` and the pins on Done, `PlanRules.cs:368–369`). Release, cancel, reopen and epoch replacement are distinct supersession cases. A decision **ends** a review rather than making it moot (§5).
9. **Saturation preserves what is established:** the G cap keeps the accepted status, checkpoints and deadline, and counter saturation is diagnostic only. Incomplete or corrupt proof fails closed separately (§6, §7).
10. **Frozen wire shapes:**
    - `workerSession` is byte-equal to `executorRef` (proposed format `hkw1:<principal>:<sessionId>`);
    - the `packageRef` JCS forms are `supplied.v1` and `stored.v1`;
    - integer limits are in §4.1;
    - `worker_rebind` is withdrawn (§4, §8).
11. **ChatAgent:** no persistence and no writes, made explicit (§9).
12. **E2c:** a proposed fixture-only scope inside §12. No tests or code GO.

## 16. Revision 5 changes (codex-hekate msg 1145)

1. **C-B request identity:** the request is either the full ExecutionKey or ReviewKey (exact byte match), or a full AttemptKey plus `runId` (or plus `artifactRef` for a review), resolved through the durable, unique-by-construction mapping. Lookups refuse on ambiguity or mismatch (`ambiguous_key`, `unknown_key`, `identity_mismatch`) and never pick one. Every response echoes the complete resolved identity (§8).
2. **Per-field proof, separate from paging:** the blanket `complete` flag is replaced by per-derived-field `proof` with fixed required-proof sets, plus `historical` page continuation (`nextCursor`, `truncated`). Retained verified anchors are required proof and are never evicted while the key is current. Caps, saturation, unrelated compaction and page truncation never erase them; missing or corrupt required proof makes only the affected fields `unknown` (§2.9, §6, §8, §9).
3. **Fixture constants only:** `hkw1`, the `packageRef` kinds and the §4.1 values are constants for the bounded E2c experiment, not accepted production identity, auth or default contracts (§4).
4. **Integer wire rules:**
   - `attemptEpoch` ≥ 1;
   - `contentRevision` ≥ 1;
   - `stateRevision` ≥ 0;
   - event `seq` ≥ 1 and `eventsAfter` ≥ 0 (each cited to `PlanStoreSchema.cs`);
   - booleans, strings, `null`, fractional and exponent forms are rejected as integers (§4.1).
5. **E2c (§12):** adds integer boundary cases, identity cases (two runs, two packages, a session mismatch, a duplicate mapping) and per-field completeness cases.

## 17. Revision 6 changes (codex-hekate msg 1148)

1. **ReviewKey resolution follows the authorized binding chain**, not `review_requested` alone. The chain is `review_requested` → `review_assigned` or the atomic bind+ACK → `review_rebind`, committed links only, in `at` order. *(Ordering by `at` is superseded in revision 7, §18.)*
   - The current binding is echoed in full, and earlier bindings appear under `historical`.
   - A superseded session gives `stale_binding`; a forked or gapped chain gives `ambiguous_binding` plus an operator entry; a corrupt chain gives `unknown`; an unbound session is echoed as `null`.
   - Bind, assign and rebind never change a deadline anchor (§8).
2. **E2c (§12)** adds review lookups before binding, after an atomic bind+ACK, after `review_assigned` and after `review_rebind`, plus the fork and corrupt-chain cases, each asserting unchanged anchors.
3. Production identity, auth and defaults remain unselected. The §4 / §4.1 values are fixture constants only.

## 18. Revision 7 changes (codex-hekate msg 1152)

1. **Binding-chain order comes from linkage, not time.** Each link names its exact `predecessorBindingId`, and the predecessor's journal append sequence is lower than its successor's. `at` is a deadline anchor only and may tie or regress.
2. **Appends are compare-and-set** inside the reviewed transaction. A link whose predecessor is not the current binding is refused as `stale_binding`.
3. **Gaps and forks fail closed:** a missing predecessor, two successors, or a non-increasing append sequence each give `ambiguous_binding` plus an operator entry.
4. **E2c (§12)** adds equal-`at` and regressing-`at` cases, showing that linkage and sequence decide the binding while the original anchors stay unchanged. It also adds the compare-and-set refusal and the gap and sequence-violation cases.
5. Still design only. Production identity, auth and defaults remain unselected.
