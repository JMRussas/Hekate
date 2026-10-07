# Plan 032 — Selective-context conversation handoff (design proposal)

**Status: design-only proposal, revision 6 (folds in source reviews msgs 1219 and 1223, lead reviews msgs 1227, 1233, 1241 and 1246, and consumer review msg 1230 as relayed in 1233; changes in §9–§13), for review by codex-hekate (lead); consumer review via the coordinator. No implementation GO.** Nothing here changes a production schema, service, provider, bridge, CLI, wake mechanism, ChatAgent or ChatRuntime, and nothing here is tested. The accepted E2c fixture ([031](031-supervisor-e2c-validation.md)) and the frozen [030](030-worker-ack-progress-review-boundary.md) rev 7 are **unchanged**. Every proposed contract needs its own review and GO.

**Task:** codex-hekate msg 1209, carrying direct user steering (msg 1191): *"we don't necessarily need to keep a monolithic conversation. We could pull across what information we need and use new conversations intelligently when it makes sense."*
**Base:** Hekate `d0ed671` plus the accepted, uncommitted E2c overlay (031 hashes).

**Labels:** **V** = verified in source; **I** = inferred; **P** = proposed here.

## 0. Problem and scope

Today an agent's working memory is its conversation. Long work stays in one conversation, which is compacted when it grows. Compaction is lossy, opaque to the supervisor, and ties continuity to a thing that can crash, stall or be replaced.

Hekate already keeps the task truth outside conversations: PlanStore holds task state, and the supervisor journal holds execution and review evidence (030, 031). This proposal makes the conversation **replaceable compute**:

1. a **fresh conversation** can take over work;
2. it receives a **selected, retrievable context package** built from the durable task and evidence, not a replayed or compacted transcript;
3. the change of **who owns the work** is an explicit, durable, compare-and-set transition. **Within an attempt** it never resets deadlines, novelty or sequence numbers. Option R (§4) deliberately ends the attempt and starts a **new** one, whose new deadlines are explicit and reported as such; it is not a reset of the old attempt.

**In scope:** identities, the package, ownership-transition options with their trade-offs, failure cases, and the smallest fixture-only next increment.
**Not in scope:** choosing production semantics (§7 lists the decisions), conversation hosting, model selection, ChatRuntime's context assembly, wake, auth.

## 1. Source facts

| Fact | Source |
|---|---|
| **V:** `workerSession` must be **byte-equal** to the attempt's `executorRef`; a worker session **cannot** be rebound inside an attempt (`worker_rebind` withdrawn); a new worker session needs a PlanStore release or reopen, i.e. a new attempt or epoch (a supersession) | 030 §4 (lines 130, 159), §15 item 10 |
| **V:** a **lead** session can change inside a review via `review_rebind`, gated on the old session's release or an operator act, ordered by predecessor linkage + append sequence with a compare-and-set; it never re-anchors a deadline | 030 §4, §8 (rev 6–7); `e1/acts.py` `View.chain` (462), `decide_binding` (699) |
| **V:** the ExecutionKey **includes** `workerSession`; worker bookkeeping (last `actSeq`, checkpoints, digests) and deadlines are keyed by `exec_id` (the full ExecutionKey); `ActId` hashes the full key | `e1/acts.py` `parse_exec_key` (149), `Act.stream_key` (191), `Act.act_id` (201), `exec_id` (244) |
| **V:** dispatch requires the claim's `executor_ref` to equal `workerSession` and correlates the authoritative claim receipt under the PlanStore project lock | `e1/acts_durable.py` `dispatch` (222–249) |
| **V:** PlanStore binds `executorRef` only at start/reopen, refuses a mismatching `executorRef` on finish/release, keeps it on Done, and clears it on release/cancel. There is no in-place executor change | `PlanRules.cs` 349–373 |
| **V:** the supplied task package is the H1 `buildPlanTaskContext` result over the claim receipt; `suppliedSha256` binds the task text only, the system/fast/deep instructions are captured separately; the H1 work package's semantic identity includes the instructions separately plus pins and source provenance | supervisor_e1 README (E1b); `e1/h1_package.py`; 030 §4 `packageRef` |
| **V:** H1 is **fixed-cost and history-free by design**: `buildPlanTaskContext` builds with `events: []` and asserts exactly one message (the package, byte for byte) with no memory, resolved/unavailable sources, turns or active tasks; when it does not fit it returns `CONTEXT_TOO_LARGE` and **nothing is truncated**. H1 therefore does **not** support optional or cross-conversation context today | ChatAgent `e0a09d9` `src/integrations/hekate/planTask.ts` 452–497 (msg 1223) |
| **V:** C-B returns, per exact key, `current` (only from one combined snapshot) and a bounded, paged `historical` section, each field with its own proof | 030 §8; `e1/acts.py` `read_cb`; 031 choice 4, 10 |
| **V:** bridge sessions are insert-only, self-described (`conversation_ref` in the context), never liveness or model identity; the bridge cannot wake an existing IDE turn; its harness worker launches a **new process** | 030 §1.5 (bridge `7a33328`) |
| **V:** ChatRuntime owns conversational context assembly (layered context, bounded selection, asynchronous summaries with retained transcript and source attribution); Hekate's context-store is a candidate durable backend. Conversation, turn, task, execution and attempt IDs must stay separate | `CHAT-CONSOLIDATION.md`; `CHATAGENT-INTEGRATION-HANDOFF.md` (lines 31, 55–56, 80–84) |
| **V:** "Do not inject all task traces or summaries into every turn. Retain provenance and distinguish proposals, claims and verified outcomes" | `CHATAGENT-INTEGRATION-HANDOFF.md` step 4 |
| **V:** run-ID uniqueness is proven only while registry rows are retained | 031 choice 11, "Not claimed" |
| **V:** ChatAgent's source store is **conversation-scoped**: `resolve` refuses a ref whose `conversationId` differs from the snapshot's, and checks the original's `contentHash`. Each record's source carries `{conversationId, eventId, messageId, contentHash}` and `provenance: "user-stated" \| "assistant-claimed"` | ChatAgent `e0a09d9` `src/app/sourceStore.ts` 11, 54–57, 63–71 (msg 1219) |
| **V:** Hekate's `ContextAssembler.AssembleFromSubject` **does** pull recent turns (`GetRecentTurns(conversationId, limit: 4)`) despite the file header "No chat history in context"; `Assemble` pulls 3. Code, not the comment, is the fact | `context-store/ContextRouter/ContextAssembler.cs` 14, 44, 62, 416, 589 (msg 1219) |

## 2. Identities (P)

Four identities, never conflated:

| Identity | What it is | Owner | Lifetime |
|---|---|---|---|
| **Conversation** (`conversationRef`) | one model context thread (an IDE turn chain, a ChatRuntime conversation, a CLI process) | the host runtime | until abandoned; **replaceable** |
| **Session** (`hkw1:<principal>:<sessionId>`) | the transport identity an agent acts under (030 §4) | the supervisor/bridge | one per conversation **by default** |
| **workerSession / executorRef** | the identity **bound to one attempt** in PlanStore and the journal | PlanStore + supervisor | one attempt (030 §4) |
| **Attempt / run** | `AttemptKey` (+ `runId`) | PlanStore / supervisor | per claim / per dispatch |

**Rule (P, a proposed conservative mapping, not an existing source fact):** a new conversation is a **new session** unless an explicit, durable transition says otherwise. Today the source knows only the transport session (`workerSession` = `executorRef`, `hkw1:…`); it has no notion of a conversation, and the bridge's `conversation_ref` is self-reported. A conversation is never treated as "the same worker" because it uses the same principal, the same bridge credential or a copied transcript. `conversationRef` is recorded as **attributed provenance** on the transition, never as an identity the supervisor validates against.

## 3. The handoff package (P)

A fresh conversation receives one **handoff package**, assembled by the supervisor from durable sources and identified by a digest.

| Part | Content | Source | Status label |
|---|---|---|---|
| **Task** (required) | the supplied task package: task text, instructions, content and prerequisite pins | H1 over the **current** claim receipt, **reused unchanged** (one fixed-cost message, no history, no truncation); its `packageRef` | `authoritative` |
| **Authority and uncertainty** (required, **never summarized or truncated**) | who owns the work now: the transition record (§4), its gate and predecessor binding, the successor session; and every **pending uncertain effect** of the predecessor: unconfirmed appends (`CommitUnknown` / `not_observed`), launches or effects without outcomes, open operator-queue reasons | the route A transition record + journal | `authoritative`; an item whose status is unknown is listed as unknown, never omitted |
| **State** (required) | the C-B `current` fields for the exact key: phase + anchor, ACK, progress (last checkpoint, count), review state, counters, queue reasons | one C-B combined snapshot | per field, under the role policy below: **mandatory-proof** fields must be `complete`; **diagnostic** fields may be `unknown` |
| **Obligations** (required) | what is open: the current deadline phase and anchor, an open review request, operator-queue reasons | derived from State | `derived` |
| **Evidence index** (required, bounded) | pointers only: accepted checkpoint ids + evidence digests, artifact refs, decision/evidence refs, binding history | journal records (`seq`, record id) | `verified` (in the validated prefix) |
| **Predecessor note** (optional, bounded) | a short handoff note from the outgoing conversation: what it was doing, what it believes is next | the outgoing session's act | `claim` — attributed, never verified, never counted as progress |
| **Retrievable on demand** | full historical pages, artifacts, prior notes | bounded per-key reads (C-B paging, artifact store) | as stored; a missing or compacted item is `unavailable`, never inferred |
| **Prior-conversation sources** (optional) | specific passages from the outgoing conversation | **never by repointing**: an old `SourceRef` keeps its original `conversationId/eventId/messageId/contentHash` and does not resolve in the new conversation (sourceStore 63). Either an **authorized import** (a copy carrying the original ref, `contentHash` and `user-stated`/`assistant-claimed` provenance, labelled `imported`), or **capability-checked retrieval** (a capability scoped to the named refs, checked by the owning runtime). Cross-conversation lookup is not assumed to exist | `imported:<original provenance>`; an `assistant-claimed` passage stays a claim |

- **Two layers, not one:** the Task part is H1 exactly as today. Everything else is a **separately versioned handoff envelope** (`handoff-envelope.v0`, P, a new contract) beside the unchanged H1 package, never merged into the H1 text and never presented as something H1 already supports. How the host places that block (a second mandatory message, a tool-retrievable object, or a future H1 revision) is decision D11.
- **No hidden authority:** ownership, gates and pending uncertain effects live only in the required Authority-and-uncertainty part. They are never carried in the optional predecessor note or in any text a runtime may summarize, compress or drop.
- **No truncation of required parts:** like H1, if the required parts do not fit the budget, the package is refused (`CONTEXT_TOO_LARGE`-style); only optional pointers may be left out.
- **Identity: one immutable canonical manifest that binds ALL supplied content** (msgs 1227, 1229). `handoffRef = candidateDigest = sha256(JCS(manifest))` (one digest, two names; §3a), where the manifest carries the content itself or the SHA-256 of each exact byte sequence delivered, never only a read basis:

  | Manifest field | Binds |
  |---|---|
  | `policy` | `"handoff.v0"`, the role (`worker` \| `lead`) and the fixture caps in force |
  | `transition` | **the requested transition, as a candidate:** pre-assigned `handoffId`, pre-assigned transition record id and binding link id, option (`review_rebind`; R/S deferred), gate and gate ref, predecessor binding id, from-session, target session, `conversationRef` (provenance). **No commit status and no commit receipt**: those come later and point at this digest (§3a), so the hash is never self-referential |
  | `task` | `packageRef` (JCS), `suppliedSha256` of the exact task text, and the separate system/fast/deep instruction digests |
  | `authority` | the **full** Authority-and-uncertainty content: every pending uncertain effect as `{kind, recordId or intentId, status: committed\|not_observed\|compacted\|unknown, since}` and every open queue reason |
  | `state` | the **exact** C-B `current` field values with their `proof`, plus the snapshot basis |
  | `obligations` | the exact derived obligations |
  | `evidenceIndex` | the exact ordered pointer list (`seq`, record id, checkpoint id, evidence digest, artifact ref) |
  | `note` | the note's SHA-256, byte length and author session, or `null` |
  | `imports` | per import: the original ref `{conversationId, eventId, messageId, contentHash}`, the original provenance, the **destination mapping** (the target conversation's import id), the imported text's SHA-256 (verified against `contentHash` before inclusion), and the authorization refs for **both** the source read and the destination use |
  | `selection` | which optional items were included, how many were omitted, and the cursor at which omission began |

  Any change in delivered content changes the digest, even when the read basis is unchanged. The manifest is stored immutably and the package is valid only with it.
- **Selection rule:** required parts are always included and bounded (fixture constants in §6). Everything else is a pointer the new conversation fetches when it needs it. **No transcript, summary or trace is injected wholesale** (handoff doc step 4).
- **One snapshot for currentness:** State, Obligations and the Evidence index come from **one** C-B combined snapshot and carry its basis. Anything retrieved later (pages, artifacts, imports) is a separate read, labelled `historical`/`as-of`, and is **never** used as ownership or currentness proof (030 §2.8). The transition itself (§4) re-reads PlanStore and the journal under its own route A lock and does not trust the package's State.
- **No accidental history:** a context assembler that pulls recent turns (Hekate's `AssembleFromSubject` does) must not be the package builder; turns from the outgoing conversation enter only as explicit, provenance-labelled imports.
- **Proof policy per role** (msg 1227; replaces revision 2's single rule):

  | Category | Worker package | Lead package | If not proved |
  |---|---|---|---|
  | **Mandatory proof at prepare** | Task (H1 over the current receipt; pins current, no `stale_content`); identity/selector agreement; the attempt still `in_progress` for the exact key; the predecessor binding is current | the review identity resolves through the binding chain to the predecessor binding; the exact ReviewKey is still a `candidate` | no candidate is prepared |
  | **Mandatory proof at commit** | the at-prepare set **re-read under the route A lock**, compared by the freshness rule (§3a); the **predecessor** binding is still current (CAS) | same | no record (`stale_candidate`) |
  | **Mandatory proof at activation** | the **successor** binding is the current binding; its committed record carries exactly this `handoffId` and `candidateDigest`; Task pins still current (no `stale_content`); the attempt still `in_progress` for the exact key | the **successor** binding is current with exactly this `handoffId`/`candidateDigest`; the exact ReviewKey still a `candidate` | the successor does not act (client obligation, §3a) |
  | **Mandatory visibility** (must be present; its *status* may be unknown) | every pending uncertain effect and open queue reason (Authority part) | same | the package is **not issued** if any item cannot be **listed**. An item whose outcome is unknown is listed as `unknown`; it is never omitted, for budget or otherwise |
  | **Diagnostic** (may be `unknown`) | ACK, progress, deadline phase and anchor, counters | ACK, progress, deadline phase and anchor, counters | carried as `{value: unknown, proof: …}`; the package is issued |
  | **Optional** | evidence-index entries beyond the cap, note, imports | same | omitted deterministically (below) and recorded in `selection` |

- **One byte and reference accounting rule** (fixture constants, proposed; msg 1241):
  - **bytes** = the UTF-8 length of the H1 Task message text **plus** the UTF-8 length of **every separately supplied instruction text** (system, fast, deep) **plus** the UTF-8 length of `JCS(envelope)` (manifest **and** `selection` included) **plus** the bytes of any other separately delivered payload not already inside the envelope (for example import text delivered outside it). A digest binds content but never substitutes for its size; nothing delivered is outside this count;
  - **references** = the number of pointer entries: evidence-index entries + pending-effect ids + queue-reason entries + import refs;
  - the **required set** (Task text and its separately supplied instruction texts, Authority-and-uncertainty, mandatory-proof State fields, Obligations, the evidence index up to G = 64 entries, and the manifest/selection overhead) must fit **≤ 64 KiB and ≤ 256 references**, else the package is **refused** (`handoff_overflow`), never shortened;
  - the **optional set** (diagnostic State values, evidence-index entries beyond G, imports, the note) is added in a fixed order (diagnostics, then evidence index by descending `seq`, then imports in request order, then the note) while it fits **≤ 32 KiB and ≤ 64 references**; the rest is omitted, and `selection` records the omitted count and the cursor, so identical inputs always give the same package and digest;
  - **total ≤ 96 KiB**, checked before any invocation.
- **Authorization (consumer, msg 1230):** a ref or hash is **not** authority. An import needs the principal to be authorized for **both** reading the source and using it in the destination conversation; that check belongs to the owning runtime (D10), not to the fixture. An import whose bytes do not hash to the original `contentHash`, whose ref was rewritten, or whose source is ambiguous is **rejected**; a missing **mandatory** evidence item refuses the package.
- **Bounds before invocation:** bytes, item counts and the context budget are checked **before** any model invocation; a successor is never started on a package that does not fit.
- **Render, import and ownership are distinct:** rendering the envelope for a model (ChatAgent: stateless, read-only) changes nothing; an import is a provenance-carrying copy and is **not** an ACK, progress or a deadline event; ownership changes only through §3a.
- **Fail closed:** C-B unavailable, a stale basis, `stale_content`, a selector mismatch, or any unproved mandatory-proof item: the package is **not issued** and the transition does not happen. The work stays with its current owner, or goes to an operator.
- **Context assembly stays with the host runtime.** Hekate supplies the package and the bounded retrieval surface; how a model's prompt is laid out is ChatRuntime's concern (CHAT-CONSOLIDATION ownership).

## 3a. Prepare, commit, activate (P; msg 1233)

The package cannot be built from a committed transition that itself needs the package's digest. So the handoff has three explicit phases:

| Phase | What happens | Authority |
|---|---|---|
| **Prepare** | The supervisor reads one C-B snapshot, checks the mandatory-proof-at-prepare items, pre-assigns stable ids (`handoffId`, transition record id, binding link id), and builds the **candidate** envelope and manifest from the predecessor binding and the requested target. `candidateDigest = sha256(JCS(manifest))`. The candidate is stored immutably | **none.** A candidate grants nothing; the target session is still foreign |
| **Commit** | One route A transaction, in this order: (a) **retry check first**: look up the pre-assigned record id; if it exists with the same `handoffId` and `candidateDigest`, return that committed record; if it exists with anything else, `conflict`; (b) re-read the mandatory facts under the project lock and compare them with the candidate by the **freshness rule** below; (c) CAS on the predecessor binding; (d) append **one** record, the `review_rebind` whose payload carries `{handoffId, candidateDigest}` under the pre-assigned record id and link id. One record per transaction (E2b-a). If (b) or (c) fails, nothing is appended and the candidate is dead (`stale_candidate` / `stale_binding`) | the new binding exists from this commit |
| **Activate** | The successor obtains a **commit receipt**: `{handoffId, candidateDigest, bindingLinkId, recordId, seq, recordHash}`, verifiable by reading that record in a C-B snapshot (chain-validated, payload equal, binding current or superseded). It then re-reads the required facts itself (a fresh C-B read) before acting. Package delivery is separate from both | the successor may act only after confirming the receipt **and** its re-read; any act before that is validated against the binding chain like any other and is refused if the binding is not yet current |

No phase claims a launch, a wake or an ACK. The receipt proves only that the binding committed with that exact candidate.

**Commit freshness rule** (msg 1241): prepare and commit are two reads, so their snapshot bases and transaction ids always differ and are **never** compared. The commit compares a **semantic set**:
- the mandatory-proof facts: the exact key and its PlanStore state class (attempt `in_progress` / review `candidate`), the Task pins (content revision, prerequisite digest, `packageRef`), selector agreement, and the predecessor binding id;
- the mandatory-visibility set: the ids and statuses of all pending uncertain effects and the open queue reasons.

Any difference → `stale_candidate`. **Diagnostic** fields (ACK, progress, phase and anchor, counters) may differ: they are delivered labelled **as of prepare** with the prepare basis, they never invalidate the candidate, and the successor's activation re-read supersedes them.

**Retry identity (new fixture wrapper behaviour, not existing semantics):** the commit record id is pre-assigned, and step (a) checks it **before** the CAS. Without that order an exact retry would see its own committed binding as current and fail the CAS with `stale_binding`. With it, an exact resend returns the committed record and a different digest under the same `handoffId` is a `conflict` (counter + queue). A bare repeated `review_rebind` without this wrapper is still `stale_binding` (existing behaviour, not dedupe).

**Activation is a client protocol obligation, not a fence** (msg 1241). Binding validation already refuses a target's acts **before** the commit (the target is not the current binding). After the commit, the successor **is** the current binding, so 030 validation accepts its acts **even if it never fetched the receipt or re-read**. E2d adds no durable activation state and claims no activation fence; "fetch and verify the receipt, then re-read" is an obligation on the successor and its host, testable only as protocol behaviour. A durable activation record would be a separate proposal.

**Crash and failure boundaries:**

| Boundary | Result |
|---|---|
| Crash **before prepare** | nothing exists |
| Crash **after prepare, before commit** | an orphan candidate with no authority; it may be discarded or committed later only if the commit-time re-read still holds (else `stale_candidate`) |
| **Commit refused** (CAS or fact change) | no record; the candidate is dead; a new prepare is needed |
| **Lost commit reply** | `CommitUnknown`: `confirm()` on the pre-assigned record id gives `committed`, `not_observed` or `compacted`. There is no blind resend; only the same idempotent append may be resent under the E2b-a rules, or an operator decides |
| **Superseded receipt** | a later rebind supersedes the binding; the receipt then verifies as `superseded`, the successor must stop, and its later acts are refused |
| **Payload-delivery failure** | the binding is committed but the successor never got the envelope: the binding stands; the **same** immutable candidate (by digest) may be redelivered, or an operator rebinds. Nothing is launched or acknowledged by the commit, and deadlines continue from their original anchors (the successor may already be overdue) |

## 4. Ownership transition options (P; none chosen)

### Reviews: already supported

A review lead's conversation rollover uses the existing `review_rebind` (030 §4, §8): release-gated or operator-gated, predecessor CAS, append-sequence ordering, no re-anchoring. No 030 change is needed for the binding itself.

**Retry identity is NOT existing behaviour** (msg 1227): a repeated `review_rebind` naming the old predecessor is refused `stale_binding`, not deduplicated. Exact retry comes from the pre-assigned commit identity in §3a (revision 3's separate `handoff_intent`/`handoff_outcome` wrapper is replaced by it, keeping one record per transaction).

### Workers: two options (both deferred from E2d)

| | **R. Release and reclaim** (existing semantics) | **S. Explicit successor binding inside the attempt** (needs a 030 change) |
|---|---|---|
| Mechanics | PlanStore `InProgress → Todo` (release) for the old attempt, then a new claim with a new `attemptId`, epoch and `executorRef` = the new session; a new dispatch with the handoff package | a new journal act `worker_succession{predecessorBindingId, toSession, gate}` with the review-chain rules (CAS, linkage + append seq, release- or operator-gated); the attempt, epoch and claim stay |
| 030/PlanStore change | none | 030: worker bookkeeping, deadlines and C-B identity must key on a **session-free execution stream** (AttemptKey + `claimKey` + `runId` + `packageRef`), with `workerSession` resolved through a binding chain; `ActId` keeps the full key. PlanStore: `executorRef` must mean a **stable supervisor-issued holder** rather than the conversation's session, **or** PlanStore needs an in-place executor change (a production semantics change) |
| Old stream | `superseded{released}`; its evidence becomes historical | continues; earlier bindings become historical |
| Deadlines | the **new attempt** starts at a fresh ACK phase. The old attempt's anchors are not reset, but **objective-level time is not preserved**: a stalled worker can be replaced with fresh deadlines | the current phase and anchor carry over unchanged; the successor inherits any overdue state |
| Novelty / seq | the new attempt starts with empty bookkeeping; old checkpoints cannot count as new progress, but appear in the evidence index | `actSeq`, checkpoints and the G digests continue; a replayed predecessor digest is `not_novel` |
| Exclusivity | a **window** between release and reclaim in which another claimer can take the node | none: the attempt never leaves the supervisor |
| Uncommitted work | lost unless the predecessor handed it off as an artifact | same: S moves ownership, not files |
| Risk | low (all mechanisms exist and are tested in 031) | a 030 identity change; must be reviewed as such |

**R is multi-step, not atomic** (msg 1227). A single predecessor CAS or one idempotent handoff record does not make release → claim → dispatch atomic. If R is pursued, each step is its own durable intent and outcome:

| Step | Stream | Intent → outcome | Unknown outcome | Interference |
|---|---|---|---|---|
| 1 | old | `handoff_intent{option: R, handoffRef}` → (closed by step 5) | `confirm()`; stop | — |
| 2 | old | `release_intent{held op key}` → `release_outcome` (PlanStore `InProgress → Todo`) | coherent `prove` of the held transition: proved / superseded / unconfirmed / intervening; **no retry from silence** | an intervening finish (Done) or cancel → abort: the handoff ends `aborted{finished \| cancelled}` and the old stream ends normally |
| 3 | new | `claim_intent` → `claimed` (new `claimKey`, `attemptId`, `executorRef` = target session) | the E1/E2a claim rules (`C1:not_observed` etc.) | **another claimer** wins the node in the window → `no_ready_work` or a foreign receipt → the handoff ends `aborted{intervening_claim}`; the work is now that claimer's; operator informed |
| 4 | new | `package_ref` + `dispatch_intent` (with the manifest) → `dispatch_outcome` | `confirm()`; stop | — |
| 5 | old | `handoff_outcome{completed \| aborted{why}, newStream, newExec}` | `confirm()` | — |

The old attempt's anchors are kept as **historical** evidence. The new attempt's deadlines are **new**, start at its own `dispatch_intent`, and C-B reports the new stream with `predecessorHandoffRef` so an objective-level view can see the replacement (D4).

**Neither option is chosen here.** R is usable now with no contract change, at the cost of fresh deadlines and the release window. S keeps deadlines and novelty honest but changes the identity model that 030 froze. A third, rejected option: reusing the old `workerSession` in a new conversation. That is exactly the "silently treat a new conversation as the same worker" case and is forbidden.

## 5. Cases (P)

| Case | Rule |
|---|---|
| **Crash of the old conversation** | Nothing proves it is quiescent. A transition is initiated by the supervisor or an operator (gate `operator`), never inferred from silence. After the transition, any act from the old session is `foreign_session` (S) or `stale` (R). Effects it already started are **not fenced** (as E2b-a): the package says so, and reconciliation is an operator act |
| **Voluntary handoff** | The outgoing session releases (gate `release`, signed by that session) and may attach a predecessor note. The note is a claim |
| **Retry / duplicate transition** | Through the pre-assigned commit identity (§3a): the same `handoffId` + `candidateDigest` returns the committed record; the same `handoffId` with a different digest is a `conflict`. A bare `review_rebind` repeat is `stale_binding` (existing behaviour, not dedupe) |
| **Prepare/commit/activate boundaries** | See the §3a table: orphan candidates have no authority, a refused commit kills the candidate, a lost reply is resolved only by `confirm()`, a superseded receipt stops the successor, and a delivery failure leaves the binding with redelivery of the same candidate |
| **Competing successors** | Two transitions from the same predecessor: CAS on `predecessorBindingId` inside the route A transaction admits exactly one; the other is `stale_binding` (proved for reviews in 031 by the two-thread race) |
| **Omitted proof** | The successor reports work the package does not show. It is a claim; novelty still needs a new checkpoint id **and** new evidence content; nothing is credited from the note. An unproved **mandatory-proof** item blocks the package; an unknown **diagnostic** field does not (§3 role policy) |
| **Stale package** | PlanStore content or pins changed since the package was built → `stale_content`; a new package is built from the current receipt. A package is valid only with the basis it names |
| **Retention** | The evidence index names records and digests. A pointer whose record was later compacted resolves `unavailable` (the stream itself is not compacted while active; 031 E2b-a rule). Run-ID uniqueness holds only while registry rows are retained (031) — R creates new runs, so this limit carries over |
| **Transition during review** | A worker transition after Done is `stale` (the attempt has ended `finished`); the review continues with the lead binding chain |
| **Deadline or novelty reset attempts** | Within an attempt, refused by construction: no transition record anchors a deadline (030 §7). R is not a reset: it ends the attempt and starts a new one whose new deadlines are explicit and linked by `predecessorHandoffRef` (§4, D4) |
| **R interrupted between steps** | Each step's unknown outcome stops the sequence for reconciliation (§4 table); there is no retry from silence. An intervening claimer or Done aborts the handoff with that reason |

## 6. Smallest next increment: fixture-only E2d (proposal; no GO requested)

**Narrowed to the package and review rollover** (msg 1227). Worker rollover (R and S) and the identity choice are **deferred** to a later, separately reviewed increment.

On the accepted E2c fixture, test-only, disposable database:

1. **Package builder and manifest (pure + live):** build a handoff package from the E2c C-B read, `package_ref` and journal pointers, with the Task part as the unchanged H1 package. Prove: the manifest binds every delivered byte (changing any State value, obligation, evidence entry, note byte, import text or provenance with the **same** basis changes `handoffRef`); deterministic `handoffRef` for identical inputs; the role policy (an unproved mandatory-proof item refuses; an unknown diagnostic field is carried; an unlistable uncertain effect refuses); deterministic optional overflow recorded in `selection`; required overflow refused (`handoff_overflow`); the Authority-and-uncertainty part lists every pending uncertain effect (an injected `CommitUnknown` ACK and an outcome-less intent appear as unknown, never dropped) and is never truncated (an over-budget package is refused, not shortened); field proof carried through (a field that is not `complete` is `unknown` in the package); the single byte/reference accounting rule of §3 (required ≤ 64 KiB / 256 refs, optional ≤ 32 KiB / 64 refs, total ≤ 96 KiB, note ≤ 1 KiB, manifest and `selection` counted); fail closed on C-B unavailable, stale basis, `stale_content`, selector mismatch; a note never counts as progress.
2. **Review lead rollover, prepare/commit/activate (live):** prepare a candidate (no authority: the target's acts are still refused); commit one `review_rebind` carrying `{handoffId, candidateDigest}` under pre-assigned ids after re-reading under the lock; verify the commit receipt in a C-B snapshot. Prove: a change in the semantic freshness set (state class, pins, a new pending uncertain effect or queue reason) between prepare and commit gives `stale_candidate` and no record, while a diagnostic change (a new ACK or counter) does not; an exact retry returns the committed record (the retry check runs before the CAS); the same `handoffId` with another digest is a `conflict`; a bare repeated `review_rebind` is still `stale_binding`; a lost reply is resolved only by `confirm()`; a later rebind makes the receipt verify `superseded`; a delivery failure leaves the binding and the same candidate can be redelivered; anchors are unchanged throughout and the successor's first progress continues novelty. Also **document, not hide**: a successor act sent after the commit but before any receipt fetch is **accepted** by binding validation (no activation fence exists).
3. **Snapshot discipline, envelope and imports (pure):** a package whose State and Evidence index came from two different reads is refused; a later retrieval never changes a package's State; the envelope is versioned (`handoff-envelope.v0`) beside an unchanged H1 package; an import keeps its original ref, `contentHash` and provenance plus a destination mapping, its bytes are verified against `contentHash`, and a rewritten, ambiguous or hash-mismatched import is rejected; a missing mandatory evidence item refuses the package; an import is not an ACK, progress or deadline event; bounds are checked before any invocation (modelled; the source/destination authorization check is a stub that must be passed explicitly; no ChatAgent change).
4. **Crash and omitted proof (review role):** an operator-gated lead transition with no quiescence proof; the old lead's later acts are refused; a note claiming unrecorded progress changes nothing.

**Deferred (a later increment, after D1/D2):** worker rollover by R (the five-step sequence, each step's unknown outcome, an intervening claimer and an intervening Done, proving the exclusivity window exists) and by S (model only, behind a fixture flag).

No production change, no PlanStore change, no wake, no ChatAgent/ChatRuntime change, no conversation launch. It needs its own GO.

## 7. Unresolved decisions (for the lead and the user)

| # | Decision | Notes |
|---|---|---|
| D1 | **R, S, or both** for worker rollover | R works today; S keeps deadlines/novelty honest but changes 030 |
| D2 | **What `executorRef` means** | today: the **transport** `workerSession` (`hkw1:…`, 030 §4); 030 does not equate it with any `conversationRef`. S needs it to be a stable supervisor-issued holder, or an in-place PlanStore executor change |
| D3 | **Who may initiate** a transition | supervisor policy, operator, the outgoing session itself, or the user |
| D4 | **Objective-level time under R** | whether a reclaim's fresh deadlines are reported against an objective budget so replacement cannot hide a stall |
| D5 | **Where `conversationRef` comes from** | ChatRuntime conversation id, bridge session `conversation_ref`, or the host; it is provenance only either way |
| D6 | **Required-context set and bounds** | what the minimal required package is per role (worker vs lead) and its size limits |
| D7 | **Predecessor note** | allowed at all, size, and whether the lead must review it before the successor relies on it |
| D8 | **Retention of package inputs** | whether issued packages pin (retain) the records they point at, or accept `unavailable` later |
| D9 | **Context-store vs journal** for durable package storage | consistent with the open storage question (028 OQ1) |
| D10 | **Prior-conversation import mechanism** | authorized copy with provenance vs capability-checked retrieval, and who authorizes it (the owning runtime, not the fixture); no auth is implemented in E2d |
| D11 | **Placement of the handoff block** | a second mandatory message, a tool-retrievable structured object, or an H1 revision; H1 today is one fixed message by design and must not be silently extended |

## 8. Not included

- Any implementation, test, schema, migration, endpoint, service or CLI.
- Changing 030 or 031, or the accepted E2c fixture.
- Launching, resuming or waking conversations; model choice; prompt layout.
- Auth for sessions, operators or users.

## 9. Revision 2 changes (msg 1223)

1. **H1 reuse is explicit (§1, §3):** the Task part is the H1 package unchanged: one fixed-cost message, no history, refuse rather than truncate (`planTask.ts` 452–497). The handoff additions are a separate, typed block; 032 does not claim H1 supports optional or cross-conversation context (new D11).
2. **Authority and pending uncertainty are required (§3):** ownership, gates, the predecessor binding and every pending uncertain effect live in a required, never-summarized, never-truncated part, not in the optional note or summarizable text.
3. **E2d case 1** adds pending-uncertainty listing and refuse-on-overflow.

## 10. Revision 3 changes (lead review msg 1227; msg 1229)

1. **Manifest (§3):** `handoffRef` is the SHA-256 of one immutable canonical manifest that binds all delivered content: the task text and separate instructions, the full authority/uncertainty content, the exact state, obligations and evidence index, the note, import payloads with original refs and provenance, the transition (predecessor/target) and the policy version. Changed content changes the digest even with an unchanged basis.
2. **Proof policy per role (§3):** mandatory proof (refuse if unproved), mandatory visibility (every pending uncertain effect is always listed, possibly as unknown, never omitted for budget), diagnostic (may be unknown) and optional (deterministic overflow recorded in `selection`). Required overflow is refused (`handoff_overflow`). Revision 2's contradiction between "carry unknown" and "fail closed" is removed.
3. **R is multi-step (§4):** five per-step intents and outcomes, unknown-outcome handling with no retry from silence, intervening claimer/Done aborts, old anchors historical, new-attempt deadlines explicit with `predecessorHandoffRef`.
4. **Retry identity (§4, §5):** a repeated `review_rebind` is `stale_binding`, not dedupe; exact retry is a proposed `handoff_intent`/`handoff_outcome` wrapper.
5. **E2d narrowed (§6):** package + manifest, review rollover with the wrapper, snapshot/import discipline, crash/omitted proof. Worker R/S and the identity choice are deferred.
6. **Wording:** §0 no longer claims a blanket never-reset (R starts a new attempt); §2's new-conversation ⇒ new-session is labelled a proposed conservative mapping; D2 distinguishes the transport `workerSession` from `conversationRef`. Revision 2's H1 and authority corrections are kept.

## 11. Revision 4 changes (lead msg 1233; consumer msg 1230 as relayed)

1. **Prepare, commit, activate (§3a):** the candidate envelope and manifest are prepared from the predecessor binding and the requested target, with pre-assigned ids, and grant no authority. The commit re-validates under the lock, CASes and appends **one** `review_rebind` carrying `{handoffId, candidateDigest}`. A separate, verifiable commit receipt binds that digest to the committed binding. The successor acts only after confirming the receipt and its own re-read. The manifest never contains its own commit status (no self-referential hash). Crash boundaries are defined before prepare, before commit, on refusal, on a lost reply, on a superseded receipt and on a payload-delivery failure.
2. **Retry identity** now comes from the pre-assigned commit record id (one record per transaction); revision 3's separate intent/outcome wrapper is withdrawn.
3. **Proof policy** splits mandatory proof into at-prepare and at-commit/activation (re-read under the lock and by the successor).
4. **Consumer requirements (§3):** a separately versioned `handoff-envelope.v0` beside the unchanged H1 package; authorization of **both** the source read and the destination use (a ref or hash is not authority); imports keep their original refs and provenance plus a destination mapping, with bytes verified against `contentHash`; rewritten, ambiguous or missing-mandatory evidence is rejected; bounds are checked before invocation; render, import and ownership are distinct, and an import is not an ACK, progress or deadline event.
5. **E2d (§6)** cases 2 and 3 are updated accordingly.

## 12. Revision 5 changes (lead msg 1241)

1. **Activation proof is its own set (§3):** prepare and commit check the **predecessor** binding (CAS); activation checks that the **successor** binding is current and carries exactly this `handoffId`/`candidateDigest`, plus the Task pins and the attempt/review state.
2. **No activation fence (§3a):** the activation re-read is a client protocol obligation. Binding validation refuses pre-commit target acts but accepts post-commit successor acts even before a receipt fetch; E2d adds no durable activation state, and the test documents this.
3. **One accounting rule (§3, §6):** bytes = H1 text + `JCS(envelope)` including the manifest and `selection`; references = pointer entries; required ≤ 64 KiB / 256 refs (refuse), optional ≤ 32 KiB / 64 refs (deterministic omission), total ≤ 96 KiB, before invocation.
4. **Commit freshness (§3a):** compares a semantic set (mandatory-proof facts and mandatory-visibility ids/statuses), never bases or transaction ids. Diagnostic changes do not invalidate; they are delivered as of prepare and superseded by the successor's re-read.
5. **Retry before CAS (§3a):** the pre-assigned record id is checked first, so an exact retry returns the committed record instead of `stale_binding`. This is new fixture wrapper behaviour.

## 13. Revision 6 changes (lead msg 1246)

1. **Accounting includes every delivered byte (§3):** the separately supplied system/fast/deep instruction texts and any separately delivered payload outside the envelope are counted in bytes (and the instruction texts in the required set). Digests bind content but never substitute for its size.
