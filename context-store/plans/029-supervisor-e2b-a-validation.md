# Plan 029 — E2b-a (durable journal experiment): validation evidence

**Status: accepted by codex-chatagent as interim review lead after independent validation of checkpoints 1004/1014; see final acceptance below.** Test-only, in a disposable database. Nothing is added to `PlanStoreSchema` or any production migration, and there is no service, worker, provider, wake, send or bridge change. **No production storage, schema or coherent-read contract is selected** (028 §2, §10).

**Date:** 2026-10-06
**Implementer:** claude-hekate. Execution model as reported by this session: **Claude Opus 5.5 (`claude-opus-5-5`)**.
**GO:** codex-chatagent msg 927 (028 §9 only). The architecture was accepted in msg 930 with conditions (a)–(g). Source reviews: msgs 941 and 945.
**Design:** [028](028-e2b-durable-journal-proposal.md) revision 2 (committed `ca672ec`).

## Source basis and environment

| Item         | Value                                                                                                                                                                                                                                                                                                                                                        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Hekate base  | committed **`ca672ec`**. Clean-source gate: `git archive ca672ec` plus **only** the owned overlay below, in `<user profile>\AppData\Local\Temp\claude\d--Git-Hekate\ad845ee3-ddf5-4018-8f3b-f217b500bf47\scratchpad\hekate-ca672ec-e2b` (temporary). `diff -rq` against the workspace showed the overlay identical. The Api was built from the clean archive |
| ChatAgent H1 | unchanged from 027: `5255daacfc670a4919f61439eb12adcb6a401920` in `<user profile>\AppData\Local\Temp\claude\d--Git-Hekate\ad845ee3-ddf5-4018-8f3b-f217b500bf47\scratchpad\chatagent-5255daa`, `node_modules` a shared directory junction (not enforced read-only). `D:\Git\ChatAgent` not touched                                                            |
| Runtimes     | uv 0.11.19; CPython 3.13.13; Node v24.21.0 (pinned H1 runtime); PostgreSQL in the owned `hekate-local` container                                                                                                                                                                                                                                             |
| Dependency   | **`psycopg[binary]>=3.2`**, locked at psycopg 3.3.6 / psycopg-binary 3.3.6 (plus tzdata). Justified (msg 930): session advisory locks, explicit transactions, concurrent connections and a killable connection-owning child process                                                                                                                          |
| Container    | owned `hekate-local` (label-verified). Lifecycle owned by claude-hekate from msg 927: started from stopped, **stopped again after the runs**                                                                                                                                                                                                                 |

**Owned overlay (SHA-256):**

| File                                                                    | SHA-256                                                            |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `scripts/local/supervisor_e1/e1/durable.py` (new)                       | `cdfcd64d3cbe3983df4d88d9f6ee1f4f803d898c5029b1ce085b5413070f5c19` |
| `scripts/local/supervisor_e1/e1/recovery.py` (new)                      | `dc0b1e8be9d9d8a74cee29bc2c3a23c6508fae3f794edbeb695ef31355787d00` |
| `scripts/local/supervisor_e1/e1/journal_child.py` (new)                 | `49359f0a3fd85f9f3c31940d0e692a9114575d14c0fa372a403bde8f35416d5f` |
| `scripts/local/supervisor_e1/e1/journal_schema.sql` (new, fixture-only) | `5f411e9cb28d1e6a56b9c9622b0cec31b2bf215f86e65fe7df712fed90f76269` |
| `scripts/local/supervisor_e1/e1/harness.py` (edited: `dsn` property)    | `44ab0905071332ccd5290890b755b4bd056c8fa5c1b9385702fafa92d244360c` |
| `scripts/local/supervisor_e1/pyproject.toml` (edited: dependency)       | `14ebc34c1b99eac5bc2a39b6eab1301f6e84a041ac7078da6b0cb11c3b9f7af5` |
| `scripts/local/supervisor_e1/uv.lock` (edited)                          | `18a166c18400717c90309a8e1a46c5b17c71ac64a9f05f0eb91b3e34cd54d984` |
| `scripts/local/supervisor_e1/tests/e2b_support.py` (new)                | `a40e65621a7eca866263b20d9defad40d7ef9a21096de58dbb8bbf619aa2f146` |
| `scripts/local/supervisor_e1/tests/test_e2b_journal.py` (new)           | `e954be936eb95e0ca5e6dd6869c4b3d6bd7809ed3528d1cd613758806451ec4b` |
| `scripts/local/supervisor_e1/tests/test_e2b_recovery.py` (new)          | `f52f8c10aebda0bef0bc141add73d1a7ee7e9f88d916923ca89059658f0f3532` |

Docs (not part of the tested overlay): this file, a `README.md` section and a status line in 028. The committed E2a files (`evidence.py`, `coherent.py`, `supervisor.py`) are **unchanged** and are reused.

## Results

| Suite (all `uv run --locked`)                                                                | Workspace     | Clean-source gate (`ca672ec` + overlay) |
| -------------------------------------------------------------------------------------------- | ------------- | --------------------------------------- |
| Default `pytest -q`: 251 earlier (E1 + E2a) + 35 `test_e2b_journal` + 32 `test_e2b_recovery` | **318 / 318** | **318 / 318**                           |
| `pytest interop` (pinned H1)                                                                 | **31 / 31**   | **31 / 31**                             |
| `pytest interop_live` (H1 plus the real Api)                                                 | **1 / 1**     | **1 / 1**                               |

All runs were by claude-hekate on the final overlay above.

**Cleanup:**

- A post-run query found 0 `hekate_plan_e1_*` databases (only root's earlier `hekate_plan_review_20261006`, untouched). The container was stopped.
- Nothing was listening on 5108 and no `journal_child` process remained.
- Two earlier _failing_ development runs had kept `.run` work folders for diagnosis (by design). They were mine and were deleted, so no `.run` remains.
- No commits were made. The unknown dirty work (`.gitignore`, `Program.cs`, `CodeService.cs`, `SemanticEdgeResolver.cs`, `Odin/langgraph_engine/`, `LOCAL-PLANNING-HANDOFF.md`, `.review_tmp/`) is untouched.

## Acceptance checks (028 §9) and evidence

| Check                                          | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. **Atomicity**                               | `test_c1_connection_closed_before_commit_leaves_no_record_and_unchanged_counters` (gives `CommitUnknown`; `confirm()` says absent; the journal digest and counters are unchanged; appending without `reacquire()` is refused `fence_lost`; the same id resent after reacquire commits exactly once). `test_c1_a_child_killed_inside_the_append_transaction_leaves_no_record`: the child's session is `idle in transaction` with the row INSERTed; after the kill the backend is gone, the record is absent, the counters are consistent and the lock is free                                                                                                                |
| 2. **Lost COMMIT reply**                       | `test_c2_lost_commit_reply_means_no_effect_then_lookup_and_idempotent_resend`: the reply is dropped after a real COMMIT; the Supervisor refuses the intent (`journal_refused:claim_intent`, `CommitUnknown`) and **no claim POST is sent**; `confirm()` finds it; an identical resend is a no-op; a different payload, expected seq or stream gives `record_id_conflict` with digest and counters unchanged. `test_c2_a_lookup_never_confirms_a_different_identity_under_the_id` (×5 identities); `test_c2_a_corrupt_stream_never_confirms_or_answers_a_duplicate`; `test_c2_a_duplicate_intent_authorizes_nothing_without_the_fence`; `test_c2_identity_ignores_the_clock` |
| 3. **Intent before effect, killed child**      | `test_c3_a_killed_child_leaves_committed_intents_and_no_outcome` ×10 (each outcome held uncommitted, and the idle state after each effect marker). At each effect marker the intent is visible from an independent connection. After the kill the committed prefix is exact and the counters are consistent. The durable-prefix label **equals** the E2a `ModelJournal` label (C1:not_observed, C2, C3, C4, C5, C6, C7:proof_missing). A restarted writer opens                                                                                                                                                                                                             |
| 4. **Append fencing**                          | `test_c4_a_second_live_instance_cannot_take_the_writer_lock` (`fence_busy`); `test_c4_after_an_operator_takeover_the_old_writers_appends_fail` (append, `verify_fence` and `reacquire` all give `fenced:epoch`; a takeover needs a reconciliationRef; a new instance runs at epoch 2; takeover rows are immutable); `test_c4_an_append_requires_the_lock_on_its_own_live_session` (correct epoch but the lock is not held by its own `pg_backend_pid`: `fenced:lock`)                                                                                                                                                                                                       |
| 5. **Effect fencing is not claimed**           | The takeover happens **after** the confirmed `finish_intent` and **before** the old process's transition. `test_c5_an_old_writers_effect_after_takeover_is_decided_by_planstore_and_reconciled`: the effect still lands (PlanStore compare-and-set decides); the journal refuses `finish_outcome`; the old live session still blocks a new instance (takeover proves no quiescence) until the operator terminates its backend; the scan gives `C7:proved`. `test_c5_a_late_effect_after_an_intervening_change_is_refused_by_planstore`: 409, and the scan gives `C7:intervening`. No automatic action                                                                       |
| 6. **Global caps across writers**              | `test_c6_two_writers_near_the_global_cap_admit_exactly_the_admissible_set`: B is observed waiting on the singleton lock (`pg_stat_activity` = `Lock`); A is admitted; B gets `global_cap`; no deadlock. `test_c6_parallel_writers_never_exceed_the_global_cap` (4 writers × 3 attempts: exactly 5 admitted, 7 refused). `test_c6_a_refused_admission_changes_nothing_and_eviction_takes_only_resolved_data`                                                                                                                                                                                                                                                                 |
| 7. **Compaction guard and retention**          | `test_c7_raw_writes_outside_compaction_are_refused` ×7 (DELETE, UPDATE, TRUNCATE, stream DELETE, takeover DELETE); `test_c7_the_compaction_flag_cannot_delete_unresolved_or_young_streams`; `test_c7_compaction_keeps_the_chain_head_then_retention_drops_the_summary`; `test_c7_resolution_needs_terminal_evidence`; `test_c7_byte_scope_is_logical_record_plus_durable_metadata`. Msg 945: `test_c7_a_corrupt_summary_is_retained_by_age_retention` ×5 (identity, head, counters, missing, oversize, each reported in `retained_corrupt`), `test_c7_a_corrupt_summary_is_never_evicted_under_pressure`, `test_c7_a_valid_summary_is_evicted_under_pressure`               |
| 8. **Corruption detection**                    | `test_c8_a_tampered_record_marks_the_stream_corrupt` (replica-role data change: appends and resolve refused, and the scan gives `proof_missing:chain:2`); `test_c8_a_seq_gap_marks_the_stream_corrupt`; `test_c8_compaction_flag_tampering_is_detected_and_the_corrupt_stream_is_retained` (not compacted; not evicted under pressure, giving `global_cap`). **Known limit:** `test_c8_known_limit_a_credentialed_writer_recomputing_the_chain_is_not_detected`                                                                                                                                                                                                             |
| 9. **Shared coherent-read adapter experiment** | The durable adapter's result equals E2a's `coherent.prove_finish` on the same state: `test_c9_proved_and_still_proved_after_an_unchanged_replay`; `test_c9_superseded_by_a_decision_and_by_a_structural_bump`; `test_c9_unconfirmed_then_intervening`; `test_c9_an_incomplete_bounded_read_is_proof_missing` ×3 (E, Z and paging: the bounded read gives `C7:proof_missing` where E2a's unbounded read says `proved`; paging alone still proves)                                                                                                                                                                                                                            |
| 10. **Bounded recovery scan**                  | `test_c10_seeded_crash_prefixes_reproduce_the_e2a_classifications_without_writes` ×6 (C1:committed_replay, C2, C3, C6, C7:proved, C8; PlanStore snapshot and journal digest unchanged); `test_c10_paging_reports_foreign_streams_and_reads_page_by_page`; `test_c10_one_scan_call_is_bounded_and_continues_with_a_cursor`                                                                                                                                                                                                                                                                                                                                                   |
| 11. **`scan(now)`**                            | `test_c11_scan_returns_intended_notifications_only_and_writes_nothing` (no notification before the deadline, then `notify_same_lead` anchored at the record's own `at`; nothing sent or written); `test_c11_a_decision_the_snapshot_cannot_verify_goes_to_the_operator_and_the_entry_is_refreshed`; `test_c11_reopen_makes_the_review_moot_and_cleans_the_queue`; `test_c11_only_the_exact_current_review_key_is_live`; `test_c11_queue_overflow_is_explicit_bounded_and_never_drops_evidence`                                                                                                                                                                              |
| §2 **Task derivations never read the journal** | `test_task_state_derivations_do_not_depend_on_the_journal`: the plan view, node events and claim lookup are identical with the journal present, adversarially edited, and renamed away; no `context-store` `.cs` file mentions `supervisor_journal`. **Scope:** these endpoints on this build                                                                                                                                                                                                                                                                                                                                                                               |

## Contract choices (msgs 930, 941 and 945)

- **Idempotency identity** (a): writer, stream, kind, the exact original payload and the expected seq; the clock is excluded. A conflicting id changes nothing.
- **SQL** (b): every value is parameterized.
- **Bounded reads** (c): stream records and node events are bounded **inside SQL** (`LIMIT` plus a window byte cutoff), never after `fetchall`. Event reads have a row cap E, a byte budget Z and a page size. An incomplete read gives `proof_missing`.
- **Fence** (d): each append checks the writer epoch **and** that `pg_locks` shows the writer lock granted to its own `pg_backend_pid()`. After a connection loss or `CommitUnknown` the adapter refuses everything until an explicit `reacquire()`, which is refused if the epoch moved. An intent append, including an idempotent duplicate, re-verifies the fence before returning.
- **Bytes** (e): **logical, not on-disk**. Each record is charged its exact E2a encoded size plus the exact JSON size of its durable metadata (id, epoch, version, intent digest, previous and own hash). A reserved slot is charged `RECORD_MAX_BYTES` plus `DURABLE_META_MAX`, which is 2351.
- **Independence proof** (f): deterministic and scoped, as in the §2 row above.
- **Corrupt data** (g, msg 945): corrupt data is retained.
  - A resolved stream is compacted only if its records validate.
  - A compacted stream is dropped, by age **or** pressure, only if `summary_problem()` passes. It checks that the summary exists, its size is bounded in SQL, it parses, its stream, writer and resolution time match, it keeps the chain head, and the counters equal its charge.
  - `compact()` reports `retained_corrupt` with reasons.
- **Lookup** (corrected for msg 954, see below): `confirm(dsn, writer, pending)` reads in one snapshot and validates the whole stream. It raises `corrupt` or `identity_mismatch`. It never reports absence as proof.
- **Scan output** (msg 941 §1): one call handles at most `max_streams` streams and returns `next_after`. Overflow keys are retained up to a fixed bound while the overflow count stays exact.
- **Queue** (msg 941 §2): every entry is charged its key bytes plus a fixed `ENTRY_CONTENT_MAX`, so an existing entry is always refreshed in place and never shows an older classification's permissions. Resolved or moot entries leave. New entries beyond Q or Y overflow explicitly. The journal evidence is untouched.
- **Review currency** (msg 941 §3): only the exact `(root, node, attemptId, epoch, artifact)` of the snapshot's node, Done for this stream's attempt, is live; every other key is moot.
- **One snapshot** (msg 941 §4): no independent read is mixed in. The review class is derived in-snapshot from `plan_node_state`: `not_done`, `candidate` or raw-stale `operator_classification`. A standing decision whose prerequisite pins need the whole graph is **`decided_unverified`**, which gives `operator_confirm_decision` and is never guessed as accepted or moot.

## Corrections after the first checkpoint (msgs 954, 959, 963, 996)

The results table above is the **first checkpoint** (code frozen in msg 952, docs in msg 958). Review msg 954 then found three blockers before codex-chatagent's live run, and msg 963 added a doc note. The fixes below were made in the workspace only; the clean gate above was left frozen.

1. **`confirm()` semantics** (954 §1). It now returns a `Confirmation(status, record)`:
   - `committed`: the stream validates and the stored record has the full pending identity. This is the **only** status that authorizes the effect that followed the append;
   - `not_observed`: the stream **exists**, validates, and shows no such record. This is **not proof of absence**. By the caller's contract only the _same_ held append (same id, payload and expected seq) may be resent, or the caller stops for an operator. The adapter does **not** enforce that rule beyond its expected-seq check;
   - `compacted`: the stream is compacted, dropped, **or not visible at all**. A missing stream may be one whose first record committed and was later resolved, compacted and dropped (msg 996), so a missing stream is **never** read as "not committed", whatever the expected seq. The outcome is unknown and only an operator reconciles it.
   - Regressions: `test_c2_a_compacted_or_dropped_stream_never_confirms_absence`, `test_c2_a_forgotten_first_claim_intent_is_operator_only_after_retention` (msg 996) and `test_c2_not_observed_carries_no_record_and_the_held_resend_commits_once`.
   - The earlier claim that "a stream that never got past its first record" reports `not_observed` was wrong (msg 996) and is withdrawn.
2. **Bounds validation before the database** (954 §2). `ReadBounds` and `OperatorQueue` reject zero, negative, bool and non-int values. `scan()` rejects a bad clock, bad windows or a bad cursor before connecting. Regressions: `test_read_bounds_reject_non_positive_or_non_int_values` ×30, `test_operator_queue_rejects_bad_bounds` ×5, `test_scan_rejects_bad_inputs_before_touching_the_database` ×7.
3. **Queue cleanup across pagination** (954 §3, ordering fixed for msg 996). Every scan call first refreshes the queue from its own reads, **then** runs `OperatorQueue.reconcile()`, whatever the cursor position, **then** counts `queued`. Reconcile checks the queued keys (at most Q, 64 per query) against the authoritative stream rows and removes any that are no longer active, so a stream resolved after this call read it is never left queued. `removed_inactive` is reported. The full-pass-only `forget_missing` was removed. Regressions: `test_queue_cleanup_and_count_work_across_bounded_scan_calls` and `test_a_resolution_after_the_reads_is_never_left_queued_by_that_scan` (a deterministic hook resolves the stream between the reads and the queue update).
4. **Byte-budget scope** (963). The counters and caps cover the journal's **logical** data only: records, reserved slots and summaries. They do **not** cover `writer_usage` rows, `takeovers` audit rows, stream bookkeeping columns, indexes or PostgreSQL storage. Those are **uncapped and retained**: audit rows are never deleted, and this experiment does not bound total journal or database size.

Results of the first correction round (checkpoint msg 992), run independently by codex-chatagent: default **363 / 363**, `interop` **31 / 31**, `interop_live` **1 / 1** (run separately). Its review probes then found the two msg 996 blockers, both failing on that checkpoint as reviewed.

Second correction round (msg 996): offline, the 42 validation tests pass, all 114 E2b-a tests collect, and the E2a model plus fixture suites give 173/173. The final independent live results, including the subsequent additive regression, follow below. The overlay hashes in the first table describe the first checkpoint.

## Final independent acceptance (2026-10-06 local)

codex-chatagent reviewed the final source and accepted the **disposable E2b-a experiment only**. A separate read-only reviewer confirmed both final blockers were resolved. The final clean gate is the `ca672ec` archive above plus the owned overlay, matched byte-for-byte to the workspace before validation. Unknown workspace changes remain excluded.

Using `uv run --locked` on that gate:

- `pytest tests -q`: **366 passed** (251 earlier tests, 39 journal, 76 recovery), zero skips.
- Two independent review probes: **2 passed**. Both failed on checkpoint 992 and now pass: first-intent confirmation after full retention drop, and resolution between classification and queue reconciliation. Probe source is `hekate-e2b-review-probes.py` in the scratchpad parent of the clean gate; it is not part of the default suite. Earlier failing-run artifacts are retained.
- The previous independent **31 pure H1 and 1 live H1** checks passed on checkpoint 992, before the final journal/queue corrections. They were not rerun for this final correction; their sources and H1 pin are unchanged. The initial combined default/live selection hit the two fixtures' shared test port; running the live suite separately passed.

Final hashes replacing the corresponding first-checkpoint entries above:

| File                         | SHA-256                                                            |
| ---------------------------- | ------------------------------------------------------------------ |
| `e1/durable.py`              | `55eef956fc7ec3a90d9e2cc57749e25809b945bade111ed65aa939370c82a6b1` |
| `e1/recovery.py`             | `927fc646d94c176c610433c9d1587ea81b8393cfbea183765287f2776a656de1` |
| `tests/test_e2b_journal.py`  | `c5b878c127976209a2876119d92a6283d96e026179d408da0dc9f624211b1707` |
| `tests/test_e2b_recovery.py` | `f2b75b5b3102b7acae2fb8280db35490c48a1f3705db860e8255891e9dea2a3b` |

The remaining six tested overlay hashes are unchanged. The additive held-sequence/fresh-id regression in checkpoint 1014 documents that the adapter does not enforce the caller's retry rule; it raises the final total from 365 to 366. The runs used `recovery.py` hash `ee0bb9937897c66810817827e2d1e5f1ddcd777ebbb110158a553879b16a710c`; the final hash above differs only in a two-line comment qualifying the snapshot boundary, edited by the review lead after validation. Executable code is unchanged.

**Read-view limit:** queue cleanup removes entries observed inactive by its reconciliation read; a resolution after that read can remain queued until a later scan. Neither queue entries nor intended notifications confer new execution authority. Missing-stream `compacted` means uncertainty, including a stream that never existed; it does not prove historical compaction. These conservative results require operator reconciliation.

**Environment:** the shared development PostgreSQL container stays running for API 5103 and UI 5179. Test cleanup owns only its generated database and API 5108, not the shared container or development services. No production journal or automatic recovery is activated by acceptance.

## Not claimed

- Production storage, migration, schema or service. A production coherent-read contract (028 open question 5).
- Network-partition behaviour: the lost reply is simulated after a real COMMIT.
- Admission throughput: the singleton serializes all admissions.
- Effect fencing; quiescence of a taken-over process.
- Tamper resistance against credentialed writers.
- Wake, scheduler or sending. Worker, provider or process launch (E3). Leases and heartbeats (3b2). Auth for operator acts.
