# Plan 028 — E2b: durable supervisor journal (design proposal)

**Status: revision 2 accepted by codex-chatagent as interim lead after reviews 914 and 919. The disposable E2b-a experiment in section 9 is independently accepted; scope and final evidence are in [029](029-supervisor-e2b-a-validation.md).** Nothing here activates a journal, schema, service, worker, provider or wake mechanism. Option A below is proposed **for a disposable experiment only**; **no production storage is selected** (msg 914).

**Builds on:**
- [026](026-launch-and-review-evidence-proposal.md) rev 5: accepted record shapes, crash points C0–C11, bounds;
- [027](027-supervisor-e2a-validation.md): the E2a in-memory model, classifier, fixture-only coherent read and supervisor hook, with corrections from msgs 901, 905, 910, 911 and 916 (required outcome evidence, degraded fallback, metadata and clock headroom, queue cleanup);
- [019](019-durable-claims-and-pins.md): PlanStore claims, receipts, append-only events and fence triggers.

**Labels:** **V** = verified in source at `95ede86`; **I** = inferred.

## 0. Scope

E2a proved *which facts suffice*, using an in-memory model with explicitly **no** durability. E2b proposes **how those facts could become durable**:
- atomic and idempotent appends;
- crash recovery;
- bounded admission and compaction;
- write fencing **as distinct from** effect safety;
- corruption detection;
- an explicit boundary for any wake or escalation trigger.

The invariants carried from 026 and 027:
- one task ledger (PlanStore);
- intent before effect;
- unknown means manual;
- worst-case reservations, metadata headroom included, before an effect;
- required outcome evidence (failed or degraded means stop);
- explicit terminal resolution;
- no automatic relaunch, retry, release or ownership transfer.

## 1. Facts that shape the choice

| Fact | Source |
|---|---|
| **V:** PlanStore runs PostgreSQL with append-only event tables, enforced by triggers, and an HP409 fence | `PlanStoreSchema.cs`; 019 §4 |
| **V:** The C7 proof needs node state and events read coherently. The public API's endpoints are independent reads. E2a used a fixture-only repeatable-read SQL snapshot | 026 rev 5 §5; `e1/coherent.py` |
| **V:** No supported wake mechanism exists; the bridge cannot resume an ended turn | bridge msgs 686 and 880/881 |
| **V:** The E2a model's bounds, reservation (metadata headroom included), planned eviction, required-evidence and resolution rules are specified and tested | 027 (msgs 892–911) |

## 2. Storage options (an experiment choice, not a production selection)

| Option | For | Against |
|---|---|---|
| **A. Append-only tables in a separate `supervisor_journal` schema in the PlanStore PostgreSQL database** (proposed **for the E2b-a experiment only**) | PostgreSQL transactions give atomic, durable commits. A **shared coherent read** of journal and PlanStore facts in one REPEATABLE READ transaction becomes possible for an experiment. Reuses the append-only trigger pattern | Couples to PlanStore availability. Needs strict schema separation, so the journal is never task state. A coherent read by direct table access is **not** a production contract (section 9, open question 5) |
| B. Local append-only file per writer (JSONL, checksums, fsync) | Independent of the database | No coherent read with PlanStore, so C7 stays `proof_missing`. Torn tails, fsync and Windows locking would all need proving |
| C. A separate database or service | Isolation | A new service; no coherent read; out of scope |

**Rule:** no task-state derivation may read the journal, and the journal never becomes a second ledger. An experiment test asserts the derivations never touch `supervisor_journal`.

## 3. Atomic, idempotent appends

- **Client-generated immutable record id.** Before sending an append, the writer generates `record_id` (a UUID) and keeps the intended record (kind, payload, expected `seq`).
  - The INSERT is idempotent on `record_id`: a duplicate with an identical canonical record is a no-op; a duplicate with different content is refused.
- **One record per transaction:**
  1. lock rows in the fixed order of section 6;
  2. check writer ownership (section 5, append fencing);
  3. admission and reservation, with metadata headroom;
  4. assign `seq`;
  5. link the hash;
  6. INSERT;
  7. update the counters;
  8. COMMIT.
- **A lost COMMIT reply is UNKNOWN** (msg 914 §2). If the commit result is not observed (connection drop, timeout), the writer **must not** retry blindly and **must not** start the effect. It performs an **authoritative lookup** by `record_id` on a new connection, then:
  - if the record is found and identical, it is committed; continue;
  - if it is not found, the writer may re-send the **same** `record_id` and the same record (idempotent) once it has re-acquired its fence, or stop for an operator. Counters are consistent either way, because they live in the same transaction as the INSERT.
- **Intent before effect:** the external effect (the claim POST, a spawn, the transition, a notify send) starts **only after** the intent's commit is **confirmed**, either by an observed COMMIT or by the authoritative lookup.
- **Outcomes never fail admission.** Their count and worst-case bytes (including writer, root, key, maximum `seq` and bounded clock metadata) were reserved in the intent's transaction. A malformed outcome is stored as bounded `invalidPayload` evidence and reported as **degraded**, which stops further effects (027).
- **No combined "intent + PlanStore write" transaction.** PlanStore writes stay on the contract API.

## 4. Crash recovery: bounded, read-only scan

On start-up, and on an operator-run scan:
1. The writer acquires its append fence (section 5). Without it, it **performs no effects**.
2. **Bounded paging:** it lists its own unresolved streams in pages of at most P streams, using a `(stream_key)` cursor. For each stream it reads the journal prefix and the node's events in pages of at most E records and at most Z bytes. It never materializes all streams or all events at once (msg 914).
3. **Per stream**, it gathers facts in **one** REPEATABLE READ transaction for that stream: the journal prefix, the `plan_node_state` row, that node's events (bounded paging inside the same snapshot) and the claim receipt. If the bounds are exceeded before the facts are complete, the stream classifies as **`proof_missing`** (incomplete), never as proved.
4. It runs the E2a classifier and writes results to an **operator queue**, a read model with its own bounds (section 6). It **performs no effect**. Streams of other writers are reported as *foreign* and never acted on.
5. **Classifications are advisory, never task truth** (msg 919). They are not persisted into PlanStore or the journal as state; any later read re-derives them from current facts. PlanStore stays the only task ledger.

## 5. Fencing: database appends vs external effects (separated, msg 914 §1)

**Append fencing (database writes only):**
- A session **advisory lock** on `hash(writer_id)`, plus a per-append compare-and-set of `(writer_id, writer_epoch)` against the stream owner row inside the append transaction.
- This guarantees that **journal appends** from a stale or fenced-out writer are refused.

**Effect fencing is NOT guaranteed by the append fence.**
- The external effects (an HTTP claim or transition, a spawn, a notify send) happen **outside** the journal transaction. Between a confirmed intent and the effect, the writer can lose its connection or lock, or an operator takeover can happen, and the effect may still be sent.
- Consequences:
  - **in-flight and late effects are UNKNOWN** after any fence loss or takeover, and reconciliation follows 026 C3, C4, C7 and C9;
  - **there is no claim** that a writer stops promptly on an *undetected* disconnect (a half-open TCP connection can hold an advisory lock until server-side detection);
  - **operator takeover cannot prove the old process is quiescent.** It fences only the old writer's future journal appends. Effects the old process sends afterwards are still possible and must be reconciled.
- Mitigations within scope:
  - PlanStore's own compare-and-set (`expectedStateRevision`, attempt and epoch) remains the final authority on task writes;
  - a writer re-verifies its fence **immediately before** each effect, which narrows the window but does not close it;
  - takeover requires an operator `reconciliationRef`.
- **Epoch bumps are operator-only** (`operator_resolution{decision: "writer_takeover"}`). There are no leases or heartbeats (3b2) and no automatic transfer.

## 6. Bounded admission, global caps and compaction (msg 914 §3)

- **Counters:**
  - one **singleton `global_usage` row**: total count and bytes over all writers, covering active records, reserved slots at worst case, resolved-uncompacted records and summaries;
  - `writer_usage`: unresolved streams per writer;
  - `stream_usage`: per stream.
- **Fixed lock order:** every admitting transaction locks `global_usage`, then `writer_usage(writer)`, then `stream_usage(stream)`, all `FOR UPDATE`, in that order, so parallel writers cannot deadlock or both pass a nearly full global cap.
- **Admission:** an intent is admitted only if its full worst-case reservation fits the per-stream, per-writer unresolved and **global** caps. Worst case means `RECORD_MAX_BYTES` per reserved slot, after the metadata-headroom check of 027.
- **Eviction:** planned first, applied in the same transaction only if admission succeeds, otherwise rolled back with no change. Only resolved data is ever evicted.
- **Compaction:** a separate guarded transaction (same lock order) for **resolved** streams older than D. It writes a summary row (identities, outcome, resolution, `reconciliationRef`, final hash-chain head), deletes the stream's records and adjusts the counters. Summaries older than R are dropped.
- **Operator queue retention:**
  - bounded by count and bytes (Q entries, Y bytes);
  - an entry is removed when its stream is resolved or becomes moot (027 queue cleanup);
  - at the cap, queue entries for **new** streams are not added and are counted, and existing unresolved entries are never evicted. The underlying unresolved journal evidence is **always retained**; dropping applies only to the queue view (msg 919);
  - overflow is surfaced **explicitly**: every scan result carries `queued` and `overflow_count` (plus the affected stream keys, up to a bound), and a non-zero overflow is itself an operator-visible alert. The queue never implies that all cases are queued;
  - the queue is a read model and is fully re-derivable by a scan.
- **Append-only guard:** a trigger refuses UPDATE and DELETE except inside a compaction transaction (`SET LOCAL supervisor_journal.compaction='on'`, re-checking resolved and old enough). TRUNCATE is always refused.
  - **These flags are not authorization** (msg 914 §4): any holder of database credentials can set them, as with the PlanStore fence (019).

## 7. Corruption detection (not tamper-proofing; msg 914 §4)

- **Validate on read:** every record is re-validated on read (E2a payload rules, record size, format `version`).
- **Per-stream hash chain:** `record_hash = SHA-256(prev_hash ‖ canonical record)`; summaries keep the head.
- **What a mismatch means:** a missing `seq`, a chain mismatch, a failed validation, or an unknown kind or version marks the stream **`corrupt`**:
  - it classifies as `proof_missing` with `operator_reconcile` only;
  - it is never auto-repaired, truncated or compacted;
  - new intents on it are refused.
- **What this detects:** **accidental** corruption and **well-behaved-path** bugs.
- **What it does not detect:** a **hostile writer with database credentials**. Such a writer can rewrite records *and* recompute the chain, or disable triggers. Tamper resistance needs an external anchor (for example a signed or remotely held chain head), which is **out of scope**.
- **Database-level corruption or loss** is an infrastructure restore. After it, a scan flags streams that are corrupt or missing.

## 8. Wake and escalation integration boundary

- **`scan(now)`:** read-only and bounded (section 4 paging). It returns **intended** notifications (due escalations at their original anchors, review candidates and accept-eligibility from current facts) and operator-queue entries.
- **Sending:** only through `notify_intent` (confirmed commit), then the transport send, then `notify_outcome`. Dedup keys and K apply.
- **Who calls `scan`** is outside E2b (026 open question 5). That caller has **no authority** to claim, finish, release, decide or reassign. **Delivery is not action**, and the bridge cannot wake an ended turn; E2b does not change either.

## 9. Bounded first experiment (E2b-a): test-only, disposable database

**Question:** in a disposable PostgreSQL database, do the durable rules in sections 3–7 hold under real transactions, concurrency and simulated crashes, with the E2a classifier unchanged?

**Shape:**
- A **fixture-only** migration creates the `supervisor_journal` tables, triggers, singleton and counter rows **in the disposable test database only**. Nothing is added to `PlanStoreSchema` or any production migration.
- A Python adapter implements the E2a hook contract over those tables, including the degraded return.

**Checks:**
1. **Atomicity:** an append whose connection is closed before COMMIT leaves no record and unchanged counters.
2. **Lost COMMIT reply** (msg 914 §2):
   - the reply is simulated lost after a real COMMIT;
   - the writer does **not** start the effect;
   - an authoritative lookup by `record_id` finds the record;
   - re-sending the same `record_id` is a no-op;
   - a different record under the same id is refused;
   - the counters are consistent.
3. **Intent before effect:** the effect starts only after the intent commit is confirmed (observed or looked up). A child Python process holding the connection is killed at each 026 crash point; the intent remains visible and the outcome is absent. No workers or providers are involved.
4. **Append fencing:**
   - a second instance cannot take the advisory lock;
   - a stale-epoch append is refused;
   - after an operator takeover, the old writer's appends fail.
5. **Effect fencing is not claimed:**
   - the test sends an effect from the **old** writer after takeover, through the fixture HTTP client;
   - the journal refuses the old writer's outcome record;
   - the effect is classified **unknown** for reconciliation;
   - PlanStore's own compare-and-set decides the task write.
6. **Global caps across writers:** two different writers admit in parallel near the global cap. The singleton lock and fixed order mean exactly the admissible set commits, with no deadlock. A refused admission changes nothing.
7. **Compaction guard and retention:**
   - a raw DELETE outside compaction is refused;
   - compaction of an unresolved or young stream is refused;
   - the summary keeps the chain head;
   - operator-queue bounds hold.
8. **Corruption detection:**
   - fixture raw-SQL tampering (deliberately using the compaction flag), a seq gap or a chain mismatch marks the stream `corrupt`, which classifies as `proof_missing` and refuses new intents;
   - a test documents that a credentialed writer recomputing the chain is **not** detected (a known limit).
9. **Shared coherent-read adapter experiment** (msg 914 §5):
   - the journal plus PlanStore facts are read in one REPEATABLE READ transaction by direct table access in the disposable database, reproducing E2a's C7 sub-cases;
   - this is an experiment of a shared adapter and **not** a production proof path;
   - incomplete bounded reads give `proof_missing`.
10. **Bounded recovery scan:** paging (P, E, Z) is respected. Seeded crash prefixes reproduce the E2a classifications. There are **no** PlanStore writes (database snapshot unchanged).
11. **`scan(now)`:** returns intended notifications only (fake clock). Queue cleanup on moot or resolved works. At the queue cap, overflow is reported explicitly and the overflowed streams' journal evidence is unchanged. No classification is persisted as task state. No sending.

**Exit:**
- checks 1–11 pass on a fresh disposable database, with database-verified no-write assertions;
- any production storage, schema, coherent-read contract or service remains a separate proposal and GO.

## 10. Not included

- A production migration, a `PlanStoreSchema` change, a service or a daemon.
- A production coherent-read contract.
- Wake, scheduler or bridge changes.
- Worker, provider or process launch (E3).
- Leases and heartbeats (3b2).
- Auth for operator acts.
- Tamper resistance against credentialed writers.
- Infrastructure backup and restore.

## 11. Open questions

1. Is a journal coupled to the PlanStore database acceptable beyond the experiment? Or must it survive a PlanStore outage (Option B, losing coherent C7)?
2. Operator acts (takeover, resolution): who is authorized, and how, before auth exists?
3. Where does the operator queue surface (a read-only endpoint, a CLI, the plan browser)?
4. Production bounds: N, M, S, B, D, R, K, `RECORD_MAX_BYTES`, P, E, Z, Q, Y.
5. A **production coherent read**: should PlanStore expose a reviewed read-only "node + events (+ journal) snapshot" contract instead of direct table access (026 open question 6)?
6. Is an external anchor for chain heads wanted later, for tamper evidence?
