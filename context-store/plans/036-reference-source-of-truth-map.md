# Plan 036 — Reference source-of-truth map (design note)

**Status: design note, revision 2 (folds in root review msg 1396), docs-only, for review by codex-chatagent (root lead, msg 1382). No implementation GO.** It maps the identifiers and facts that the accepted 019/026/028/030–035 work already defines to the store that owns them, the exact scope of each hash, the snapshot semantics of the receipt and the `Fresh` proof, the refusal codes, and the production limits. It mints **no** identifier or encoding, adds no resolver, accepts no new wire schema and changes no code, schema, fixture, bridge or ChatAgent file.

**Field names:** names in `code` are the **accepted** names as they appear in the source cited (Python dataclass fields are snake_case, e.g. `Fresh.candidate_digest`; wire/JSON fields are camelCase, e.g. receipt `candidateDigest`). Where this note uses a name only to describe a meaning, it says "semantic". No host-side naming proposal is accepted by this note.

**Task:** codex-chatagent msg 1382 (follow-up to advisory msg 1379, which this note corrects; see §7).
**Base:** Hekate `d0ed671` + the accepted, uncommitted E2c/E2d/E2e overlay (031, 033, 035) and the accepted fixtures `e2e-consumer-v0` (INDEX `6093034b…`) and `e2e-byte-compat-v0` (INDEX `5ee9ff70…`).

**Labels:** **V** = verified in source in this session; **I** = inferred; **P** = proposed here.

## 1. Two owners, one production

| Owner | Status | Owns | Source |
|---|---|---|---|
| **PlanStore** | **Production** code (plan-contract v1, loopback-gated API) | plans, nodes, task content and its revision, dependencies, work status, the **attempt** (`attemptId`, `attemptEpoch`, `executorRef`, pins), **claim receipts**, the attempt event log, acceptance decisions | `context-store/PlanContracts/PlanStore.cs`, `PlanRules.cs`, `PlanStoreSchema.cs`; `context-store/Api/PlanContractEndpoints.cs` |
| **Supervisor journal** | **Fixture-only**: `supervisor_journal.*` in disposable test databases; no production storage selected (028 OQ1) | dispatch (`runId`), worker/review acts, progress checkpoints, deadline phases, the review binding chain, handoff candidates, commit records | `scripts/local/supervisor_e1/e1/{durable,evidence,acts,acts_durable,handoff,handoff_durable,consumer,consumer_durable}.py` and the `*_schema.sql` files |

**V:** every PlanStore writer takes `pg_advisory_xact_lock(hashtext('hekate-plan-project:' || project))` (PlanStore.cs 132 create, 235 reconcile, 346 claim, 538 mutate). The journal's route A takes that same project lock **first**, then the journal locks (030 §2). The journal never writes PlanStore tables; it reads them inside its own snapshot (`acts_durable.read_facts`).

**Consequence (V):** today the only production-backed reference is the PlanStore claim/attempt identity. Execution keys, review keys, bindings, handoffs and receipts exist only as accepted **fixture contracts**.

## 2. Reference tuples and their components

Lengths are as each validator counts them. **Python `len` counts code points, C# `.Length` counts UTF-16 code units**, so the two disagree on non-BMP characters (V).

| Component | Owner | Format and bound | Validator |
|---|---|---|---|
| `rootId`, `nodeId` | PlanStore | lowercase uuid, 36 ASCII (journal wire); PlanStore `uuid` column | `acts.UUID_RE`; schema |
| `claimKey` | PlanStore | `[A-Za-z0-9._~-]{1,128}`, not `.`/`..` | `PlanStore.IsValidClaimKey` (310); `plan_claim_receipts` CHECK; `acts.CLAIM_KEY` |
| `attemptId` | PlanStore (caller-chosen) | **any** non-whitespace string; **not ASCII-only**. Claim route: ≤ 256 UTF-16 units (PlanStore.cs 332). Journal wire: ≤ 256 code points (`acts.parse_attempt`). **Transition route: no length check found** (PlanRules.cs 344–345 checks only `IsNullOrWhiteSpace`) | see §7 finding F1 |
| `attemptEpoch` | PlanStore | bigint ≥ 1 for a started attempt; journal wire ≤ 2^53−1 | PlanRules 346; `acts.wire_int` |
| `executorRef` | PlanStore | 1–256 printable ASCII `!`–`~` | `PlanRules.IsValidExecutorRef` (462) |
| `artifactRef` | PlanStore (node) | journal wire: 1–512 printable ASCII | `acts.parse_review_key` |
| `runId` | journal (dispatch) | 1–256 printable ASCII; unique only among **retained** dispatch rows (031 choice 11) | `acts.parse_exec_key`; `run_id_reused` |
| `packageRef` | journal wire (built from PlanStore pins) | canonical JSON `supplied.v1` (hashes, `contentRevision`, `prereqDigest`) or `stored.v1` (`storeRef` ≤ 256 printable ASCII + `sha256`) | `acts.parse_package` |
| `workerSession`, `leadSession` | journal | `hkw1:<principal ≤ 64>:<uuid>`, ≤ 256 | `acts.SESSION` |
| `reviewId` | journal (derived) | 64 hex = digest of `AttemptKey + artifactRef` | `acts.review_id` |
| `prepareId` | caller of E2d prepare | lowercase uuid | `handoff.handoff_ids` |
| `handoffId`, `recordId`, `linkId` | journal (derived) | uuid5 under `HANDOFF_NS`: `handoffId` over `[root, claimKey, reviewId, predecessorLinkId, targetSession, prepareId]`; `recordId`/`linkId` from `handoffId` | `handoff.handoff_ids` |
| `candidateDigest` | journal | 64 hex | §3 |
| `seq`, `recordHash` | journal | `seq` bigint, schema up to 2^63−1 (lossless, **not** a JS Number above 2^53); `recordHash` 64 hex | `durable.py`; receipt |

The **tuples** in use:

| Tuple | Fields | Names | Defined in |
|---|---|---|---|
| **Claim (assignment)** | `{rootId, claimKey}` | one durable claim receipt: either `claimed` (node, attempt, pins, `eventSeq`) or `no_ready_work` (no node) | 019; `plan_claim_receipts` PK |
| **AttemptKey** | `{rootId, nodeId, attemptId, attemptEpoch}` | one attempt of one node | `acts.ATTEMPT_FIELDS` |
| **ExecutionKey** | AttemptKey + `{claimKey, runId, packageRef, workerSession}` | one dispatched run of a claimed attempt | `acts.EXEC_FIELDS` (030) |
| **ReviewKey** | AttemptKey + `{artifactRef, lead, leadSession}` | one review of one artifact, bound to a lead session (or none yet) | `acts.REVIEW_FIELDS` (030) |
| **Handoff** | `{handoffId, candidateDigest}` (+ `recordId` once committed) | one immutable prepared candidate; one commit record | 032/033 |
| **Commit receipt** | `{handoffId, candidateDigest, bindingLinkId, recordId, seq, recordHash}`, closed schema | one `review_rebind` record | `handoff_durable.receipt`; `consumer._closed_receipt` |

A blanket "≤ 256 ASCII" bound for references is **wrong** (correction of msg 1379): `attemptId` is not ASCII-limited, a full AttemptKey is ≥ 36+36+1+1 characters before `attemptId`, and the composed ExecutionKey/ReviewKey exceed 256 (`artifactRef` alone may be 512, `packageRef` is a JSON document). Any reference bound must be derived from the component bounds above.

## 3. Exact hash scope

`candidateDigest = sha256(py-canon.v0(manifest))` (`handoff.build`, last line). **V.** It covers the manifest's bytes and nothing else directly.

| Item | How it is bound | Bound by `candidateDigest`? |
|---|---|---|
| Manifest: `policy`, `transition` (incl. `handoffId`, `recordId`, `linkId`, `prepareId`, `root`, `claimKey`, `predecessorBindingId`, `fromSession`, `targetSession`, `gate`, `gateRef`, `conversationRef`), `task` digests + `packageRef`, `authority` (pending, queue), `state` (mandatory identity, PlanStore class, pins, snapshot `basis`), `obligations`, `evidenceIndex`, `optional` (diagnostics, evidence, import metadata with `textSha256`, note `sha256`/`bytes`/`author`), `selection` | in the hashed bytes | **directly** |
| Envelope bytes | `verify_stored`: must equal **exactly** `py-canon({version, manifest, payload{imports, note}})` of this manifest | **transitively**, through the validated relationship |
| Payload import texts and note text | each must hash to the manifest's `textSha256` / note `sha256` | transitively |
| Task bytes (`text`, `instructions{system, fast, deep}`, `packageRef`) | closed shape, canonical bytes, `sha(text)` = `suppliedSha256`, instruction hashes, `packageRef` equal | transitively |
| Receipt `handoffId`, `recordId`, `bindingLinkId` | must equal the manifest's `transition` values (`receipt_mismatch`) | transitively (equality) |
| Receipt `candidateDigest` | must equal `sha(manifest)` and the wrapper's digest (`digest_mismatch`) | yes (equality) |
| Receipt `seq`, `recordHash` | **not** in the manifest (assigned at commit) | **no**: checked only against the journal record, in the revalidation snapshot (`receipt_status`) |
| Wrapper fields (`wrapper`, `codec`) | exact string equality with constants (`codec_unsupported`) | **no** |
| H1 input instructions (`systemInstruction`, `roleInstructions.fast`, `.deep`) | closed option set and caps (`h1_input`, `ingress_too_large`), then **required equal** to the committed Task's instructions **before** H1 is called (`task_mismatch`, consumer.py 432–434) | **not directly**; equality with the transitively bound Task is required |
| H1 input `response`, `rules`, `budget`, `capturedAtIso`, `limits` | closed option set and caps only | **no**: locally supplied by the consumer's caller |
| H1 output | **after** the call: `text` and `suppliedSha256` must equal the committed Task / manifest `task.suppliedSha256`, and the instruction digests of the H1 context must equal manifest `task.instructions` (`task_mismatch`, consumer.py 474–479) | **not directly**; equality with bound values is required |
| `Fresh` proof | its fields `candidate_digest`, `record_id`, `review_identity`, `package_ref` must equal this delivery's receipt `candidateDigest`, `recordId`, the manifest review identity and the Task `packageRef` (`fresh_mismatch`) | **no**: it is about the candidate, not inside it |
| Consumer view | own `viewDigest`, outside its own preimage, fixed-width reservation | **no** (separate identity) |
| `basis` (snapshot id inside the manifest) | hashed, but deliberately **excluded** from the commit-freshness semantic set (`handoff.semantic_set`) | hashed, not compared for freshness |

Digests are computed over **received bytes** only; the canonical-shape comparison (`verify_stored`) is a separate stage. A byte hash never promotes a value (byte-compat supplement README).

## 4. Snapshot semantics

| Read | Snapshot | Returns | Authority |
|---|---|---|---|
| PlanStore `GET /plans/{root}/claims/{claimKey}` (`ReadClaimAsync`, PlanStore.cs 422) | one REPEATABLE READ transaction | the stored receipt + `stillCurrent` (receipt attempt is the node's current InProgress attempt; pins, content revision, content digest and prerequisite digest all equal) + `current {work, attemptId, attemptEpoch}` | **none**: "factual correlation only, never authority" (PlanStore.cs comment) |
| PlanStore `GET /nodes/{node}/attempts/{attemptId}/trace` (plan 047; `AttemptTraceSources.ReadAsync`) | one REPEATABLE READ transaction for node, attempt events, claim receipt and the journal's `launch_intent`/`exited`; the trace files are read afterwards, outside it | one page of the attempt's retained trace with `status` and `integrity` | **none**: shows retained worker output only; acceptance stays the node's recorded decision |
| E2c C-B read (`acts.read_cb`) | one combined snapshot over PlanStore facts + the journal stream | identity, current section, per-field proof, counters, queue | as-of only |
| E2d prepare (`handoff_durable.prepare`) | **one** REPEATABLE READ READ ONLY snapshot for facts, stream, outstanding reservations, queue; `build` refuses mixed bases (`mixed_snapshot`) | an immutable candidate; **grants nothing** | none |
| E2d commit (route A) | write transaction under project lock + journal locks; order: retry check on pre-assigned `recordId` → semantic-set freshness → predecessor CAS → one `review_rebind` record | `committed` / `accepted` / `stale_candidate` / `stale_binding` / `conflict` / `refused` | the commit is the only effect |
| `verify_receipt` | one snapshot: record under pre-assigned id, chain-validated | `current` / `superseded` / `mismatch` | as-of only |
| E2e `Fresh` (`consumer_durable.fresh`) | **one** REPEATABLE READ READ ONLY snapshot; the receipt status is evaluated **inside** it (not via a separate `verify_receipt`) together with the current binding, review class, pins problem, pending effects and queue; result names the candidate it was read for | accepted Python dataclass `consumer.Fresh` (snake_case): `candidate_digest, record_id, review_identity, package_ref, receipt_status, current_binding, review_class, pins_problem, pending, queue, basis`. A host port's naming is the host's own and is not accepted here | **as-of only; no invocation fence** (034 §6) |
| E2e retrieval (`consumer_durable.retriever`) | one snapshot per call; whole stream read and chain-validated; must be the verified delivery's own `(root, claimKey)` and match the **full** manifest pointer | bytes or `None` | none |

Lifetimes differ (V):

| Reference | Lifetime |
|---|---|
| Claim receipt `{rootId, claimKey}` | **no delete path** in PlanStore code; its FKs (to the plan, node and event) are `ON DELETE RESTRICT`, and `managed_plans` is trigger-guarded (PlanStoreSchema.cs 136–160, 314). Replays idempotently. `stillCurrent` is false unless the receipt's attempt is the node's current InProgress attempt with equal pins, content revision, content digest and prerequisite digest (PlanStore.cs `Correlate`); always false for `no_ready_work` (**I**: no node) |
| AttemptKey | permanent as history (events); **current** only until finish/release/cancel/reopen; a reopen gives a new epoch (and possibly a new `attemptId`) |
| ExecutionKey `runId` | unique only while its dispatch row is retained; compaction can make it reusable (031 choice 11) |
| ReviewKey binding | current until the next `review_rebind`; older links stay in history (`superseded`) until compaction |
| Handoff candidate | immutable: `guard_e2d` refuses UPDATE and TRUNCATE always and DELETE unless `supervisor_journal.compaction` is `on`/`evict`. It is deleted **only** by `ON DELETE CASCADE` when its **stream row** is dropped: a summary older than R in `compact` (mode `on`, durable.py 711–712) or a resolved stream under global-cap eviction (mode `evict`, durable.py 607, 633–635). **Compaction to a summary keeps the stream row, so it keeps the candidate.** Unresolved and corrupt streams are never compacted or evicted |
| Commit record / receipt | the `review_rebind` record is deleted when its resolved stream is **compacted to a summary** (durable.py 643), even though the candidate survives; from then on `receipt_status` cannot find the record and returns `mismatch` (consumer: `receipt_not_current`). While the record is retained, the receipt is `current` only while its link is the current binding, else `superseded` |

## 5. Refusal codes (existing; V)

| Layer | Codes |
|---|---|
| PlanStore claim/read | `invalid_input`, `plan_not_found`, `claim_not_found`, `invalid_executor_ref`, `operation_key_reused`; receipt outcome `no_ready_work` |
| E2c resolution | `unknown_key`, `ambiguous_key`, `identity_mismatch`, `stale_binding`, chain status codes, `run_id_reused`; wire `Malformed` (`string`, `format`, `fields`, `shape`, `package_kind`, `package_not_jcs`, `request`) |
| E2d prepare/commit | `prepare_id`, `mixed_snapshot`, `mandatory_unproved`, `stale_content`, `pending_unlistable`, `note_too_large`, `handoff_overflow`, `import_ambiguous`, `import_rewritten`, `import_hash_mismatch`, `import_unauthorized`, `conflict`, `candidate_unavailable`, `tampered_candidate`; decisions `stale_candidate`, `stale_binding`, `refused` |
| E2e consumer | `ingress_too_large`, `codec_unsupported`, `receipt_shape`, `digest_mismatch`, `strict_json`, `h1_input`, `delivery_mismatch`, `receipt_mismatch`, `delivery_overflow`, `fresh_mismatch`, `receipt_not_current`, `binding_moved`, `review_not_candidate`, `stale_content`, `uncertainty_overflow`, `uncertainty_unlistable`, `candidate_unavailable`, `retrieval_request_malformed`, `retrieval_request_too_large`, `h1_refused`, `h1_unavailable`, `CONTEXT_TOO_LARGE`; per-item statuses `denied{rule}`, `unavailable{reason}`, `omitted{budget}` (not refusals) |

## 6. Using the existing tuples as references — **recommendation only (P)**

No new string encoding is proposed (an earlier `hk-*` example, msg 1379, is withdrawn as unnecessary). The recommendation is to reuse the **existing structured tuples** of §2, unchanged:

- **P1. Assignment:** the existing PlanStore claim identity `{rootId, claimKey}`, the only production-backed, durable, idempotent reference. Resolution is the existing `GET /plans/{rootId}/claims/{claimKey}`; a holder never derives state from the tuple.
- **P2. Handoff:** `{handoffId, candidateDigest}` (the receipt adds `recordId`, `bindingLinkId`, `seq`, `recordHash`), fixture-only until 028 OQ1 selects journal storage.
- **P3.** AttemptKey, ExecutionKey and ReviewKey stay structured exact-field objects, as the accepted wire already carries them: `attemptId` is not ASCII-limited and unbounded on the transition route (F1), `artifactRef` ≤ 512, `packageRef` is JSON.
- **P4.** Each field is compared by exact value; no case folding (uuids are lowercase on the journal wire; **I** for PlanStore output); unknown, stale or moved is a typed refusal, never a guess.

## 7. Findings from this audit

- **F1 (V, production gap):** `attemptId` is length-checked on the claim route (≤ 256 UTF-16 units) but **not** on `POST /nodes/{id}/transition` (PlanRules.cs 344–345). A transition-started attempt may carry an `attemptId` the journal wire refuses (`string`). Not fixed here; flagged for the owner.
- **F2 (V):** Python (code points) and C# (UTF-16 units) count `attemptId` length differently: an astral character counts 1 vs 2. Values between the two limits pass the journal wire but are impossible from the claim route.
- **F3 (correction of msg 1379):** `candidateDigest` binds the manifest bytes only; envelope, task, imports and note are bound **through the validated relationships** of §3; receipt `seq`/`recordHash`, wrapper, H1 input and `Fresh` are **not** bound by it.
- **F4 (correction of msg 1379):** no blanket 256 limit; see §2. The `hk-*` string examples of msg 1379 are withdrawn (their prefix counts were also off by one).
- **F5 (V):** a handoff candidate is immutable but **not permanent**, and it can outlive its commit record: compaction deletes the record (receipt then `mismatch`) while the candidate stays; dropping the stream deletes the candidate (`candidate_unavailable`). A handoff reference must allow both.

## 8. Not in scope

A resolver, any new identifier or encoding acceptance, PlanStore changes (including F1), journal production storage, bridge, ChatAgent, ChatRuntime, auth, launch or invocation.
