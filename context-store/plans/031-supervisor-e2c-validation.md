# Plan 031 — E2c (worker ACK, progress and review obligation): validation evidence

**Status: accepted by codex-hekate for the bounded E2c fixture experiment, 2026-10-07.** Acceptance includes the retained-identity limitation documented below; lifetime run-ID uniqueness remains unproved. Test-only, fixture-only, in a disposable database. Nothing is added to `PlanStoreSchema` or any production migration. There is no service, worker, provider, wake, send, bridge or ChatAgent change. **The `hkw1` session format, the `packageRef` forms, G/V/K and the windows are fixture constants**, not accepted production identity, auth or default contracts (030 §4, msg 1145).

**Date:** 2026-10-07
**Implementer:** claude-hekate. Execution model as reported by this session: **Claude Opus 5.5 (`claude-opus-5-5`)**.
**GO:** codex-hekate msg 1161 (fixture-only E2c from frozen 030 rev 7 §12). Design reviews: msgs 1163, 1166, 1170. Draft and final reviews: msgs 1175–1177, 1179–1181, 1185, 1188–1190. Follow-up steering: msg 1191.
**Design:** [030](030-worker-ack-progress-review-boundary.md) revision 7, sha256 `52ef6784afdd2a8cd5619e8c042d635a7a343bc7af611fe41b1f29ba8e43ab3a` (untracked; unchanged by this work).

## Source basis and environment

| Item | Value |
|---|---|
| Hekate base | committed **`d0ed671`**. Clean-source gate: `git archive d0ed671` plus **only** the owned overlay below, in the session scratchpad (`…\scratchpad\hekate-d0ed671-e2c`, temporary). `diff -rq` against the workspace showed the overlay identical. The gate's Api was built from the clean archive. This matters: the workspace has **unowned, uncommitted** changes in `context-store/Api/Program.cs` and `Services/CodeService.cs` that the workspace Api build includes and the gate excludes |
| Runtimes | uv 0.11.19; CPython 3.13.13; psycopg 3.3.6 (already locked by E2b-a; no dependency change); PostgreSQL in the owned `hekate-local` container |
| Prerequisites (msg 1163) | Docker Desktop was not running on fenrir; started it (engine 29.8.1). Inspected the only `hekate-local` container `cb53ec16b9d3` (`hekate-local-postgres-1`: compose project `hekate-local`, `com.hekate.local.workspace=D:\Git\Hekate`, `127.0.0.1:5434` only, volume `hekate-local_pgdata`) and ran `docker start` on it **only**. `hekate-local.ps1 start` was **not** run (it also starts Api 5103). No volume, config, dispatcher or other service change. The container is **left running** for independent review |

**Owned overlay (SHA-256):**

| File | SHA-256 |
|---|---|
| `scripts/local/supervisor_e1/e1/acts.py` (new) | `1d44e39c015be3d259cd359e284e9e6b999d369f58f80e4b4a57d698ed8e80e5` |
| `scripts/local/supervisor_e1/e1/acts_durable.py` (new) | `5ad500d623e8e85f8a107e7846bde1ac810cf8ce3242fa9b3402c70dce684a44` |
| `scripts/local/supervisor_e1/e1/acts_schema.sql` (new, fixture-only) | `cbe901cba3970e08e4edc83eecc829ad40d130fa70f03e411fb056f378cccf7b` |
| `scripts/local/supervisor_e1/e1/evidence.py` (edited: vocabulary only, +6/−1) | `f8affab90705d6229e12e2bb990d6ee57fba3244c36a0be46f82ffd79f77cbde` |
| `scripts/local/supervisor_e1/tests/test_e2c_model.py` (new) | `d3309e78dbadec83bb57da49ebd7c91d6b702d012b13c5df2d3eb0dc711c5ce3` |
| `scripts/local/supervisor_e1/tests/test_e2c_live.py` (new) | `088df326853dcbbb1dcbb31695bf7edea5f20495b256f7902d49bf234799f647` |
| `scripts/local/supervisor_e1/README.md` (edited: E2c section appended) | `e2569e2bbbe67143206dfd5a48fd8864365be31a2ca59d1d4e93d0e1484fcc38` |

`durable.py`, `recovery.py`, `coherent.py`, `supervisor.py`, `journal_schema.sql`, the harness and every existing test are **unchanged** and reused.

## Results

| Suite | Workspace | Clean-source gate (`d0ed671` + overlay, `uv run --locked`) |
|---|---|---|
| Baseline before any E2c change: `uv run pytest -q` | **366 passed** in 54.42s | — |
| `tests/test_e2c_model.py` (offline) | **91 passed** in 0.12s | included below |
| `tests/test_e2c_live.py` (live) | **27 passed** in 23.64s | included below |
| Full default suite incl. the fault suite: `pytest -q` | **484 passed** in 72.35s | **484 passed** in 72.71s |

484 = the 366 pre-existing tests (unchanged, all passing) + 91 model + 27 live. Checkpoint 2 (msg 1183: 480 passed) was reproduced independently by codex-hekate from clean source (msg 1188: 480 passed in 67.59s). Its two extra probes found the gaps fixed in choices 5 and 10 below.

## The 030 §12 cases and where each is proved

| §12 case | Model (`test_e2c_model.py`) | Live (`test_e2c_live.py`) |
|---|---|---|
| 1. Shapes and limits: partial keys, non-JCS / extra / missing / unknown-kind `packageRef`, out-of-range ints, oversize payload, null or non-conforming `executorRef`, session ≠ `executorRef`; integer boundaries (epoch and `contentRevision` 0 rejected, 1 accepted; `2^53−1` vs `2^53`; `true`, `false`, `"1"`, `null`, `1.0`, `1.5`, `1e3` rejected) | `test_partial_keys…`, `test_package_ref_wire_shape[9]`, `test_representative_full_supplied_package…`, `test_attempt_epoch_bounds[7]`, `test_integer_tokens_are_rejected_never_coerced[9]`, `test_integer_maximum_is_accepted`, `test_oversize_payload…`, `test_null_nonconforming_or_other_executor_ref…[4]`, `test_an_act_for_another_stream_is_malformed` | `test_dispatch_is_correlated_with_the_authoritative_receipt`, `test_duplicate_conflict_and_malformed…` |
| 2. §6 outcomes, each counter only with no deadline effect | `test_section6_outcomes_are_counter_only…` (stale, stale_content, duplicate, conflict, re-ACK conflict, out_of_order, not_novel ×2), `test_duplicate_returns_the_original_record`, `test_progress_before_ack…`, `test_novelty_unknown_fails_closed…` | `test_stale_content_refuses_new_acts…`, `test_duplicate_conflict_and_malformed_persist_without_records` |
| 3. Deadline phases: ACK → first progress (anchored at the effective ACK) → progress; same for review; a late act anchors the next phase at its own `at` | `test_worker_phases…`, `test_ack_overdue_then_satisfied`, `test_one_live_deadline…`, `test_review_phases_and_late_act_anchoring` | `test_route_a_phases_and_c_b_from_one_snapshot` |
| 4. `confirm_act` anchors at the original observation `at`; already past = overdue at once | `test_confirm_act_anchors_at_the_original_observation_at…`, `test_confirm_reruns_novelty_and_identity` | `test_route_b_observation_is_audit_only_and_confirm_keeps_the_original_at` |
| 5. Route B audit-only; `superseded_as_of_read`; matching revisions give no `current` | `test_route_b_observations_never_advance…` (V cap), `test_matching_revisions_from_independent_reads…`, `test_superseded_as_of_read…` | `test_independent_facts_can_never_make_an_effective_act` |
| 6. Restart: anchors at durable `at`, never "now"; pending reviews survive | `test_rehydrating_from_the_records_keeps_original_anchors` | `test_restart_keeps_original_anchors_and_pending_review` (a new writer session much later) |
| 7. Stream ends: matching finish by **another actor** is `finished`; release / cancel / reopen / epoch replacement are distinct; review ends on an exact decision, moot otherwise | `test_matching_finish_by_another_actor…`, `test_supersession_cases_are_distinct[4]`, `test_review_ends_on_an_exact_decision…`, `test_a_decision_against_another_identity_is_operator_classification…[4]` | `test_matching_finish_is_finished_and_late_acts_are_stale`, `test_release_and_reopen_are_distinct_supersessions`, `test_cancel_is_its_own_supersession`, `test_exact_decision_ends_the_review_with_acceptance_unverified` |
| 8. Saturation: G cap keeps established status/checkpoints/deadline; counter saturation diagnostic only; corrupt bookkeeping fails closed separately | `test_g_cap_preserves…` (G = 64 exactly), `test_counter_saturation_is_flagged…`, `test_damaged_progress_bookkeeping_hides_only_progress`, `test_damage_reaching_the_ack…` | `test_g_cap_with_a_full_supplied_package` (64 + overflow, full `supplied.v1`), `test_counter_guards_and_saturation`, `test_damaged_progress_record_hides_progress_only_and_is_retained` |
| 9. Bindings: atomic bind+ACK and its refusal; `review_assigned` only when unbound; rebind needs a gate and never re-anchors; no worker rebind; session-distinct ActIds after rebind without resetting seq/novelty | `test_atomic_bind_and_ack…`, `test_rebind_never_reanchors_and_needs_a_gate`, `test_review_assigned_only_for_an_unbound_review`, `test_there_is_no_worker_rebind_inside_an_attempt`, `test_rebind_gives_session_distinct_act_ids…` | `test_review_lookup_after_atomic_bind_and_rebind_with_cas` |
| 10. C-B: identity (two runs, two packages, session mismatch, duplicate mapping, unknown run, review lookup), binding chain (null before binding, after bind / assign / rebind, `stale_binding`, fork, gap, non-increasing seq, equal and regressing `at`), per-field completeness, paging, journal down | `test_identity_two_runs…`, `test_single_run_attempt_lookup…`, `test_duplicate_run_mapping_is_ambiguous…`, `test_malformed_requests…`, `test_review_chain_before_binding…`, `test_review_assigned_lookup…`, `test_forked_or_gapped_chain…`, `test_equal_and_regressing_at…`, `test_successor_with_non_increasing_sequence…`, `test_per_field_proof_missing_ack_outcome_vs_corrupt_progress`, `test_truncated_historical_page…`, `test_compacting_an_unrelated_stream…`, `test_stale_basis…`, `test_missing_or_unrepresentable_planstore_facts…`, `test_page_limits` | `test_c_b_is_one_snapshot_even_if_planstore_moves_mid_read`, `test_stale_cas_after_the_winning_commit`, **`test_competing_rebinds_race_through_the_single_writer_boundary`**, `test_equal_and_regressing_at_resolve_by_linkage`, `test_journal_outage_returns_the_plan_leaf_with_evidence_unknown`, `test_unrelated_stream_compaction_changes_no_field` |
| 11. Transport ACK, `launched`, `completed` change nothing; bridge behaviour reused, not re-tested | `test_transport_ack_launched_and_completed_change_nothing` | `test_transport_facts_change_nothing` |

Also live: `test_planstore_writers_wait_for_the_route_a_transaction` shows a competing PlanStore finish (through the real Api) **blocks** on the project lock while a route A transaction is open, and commits only after the act is durable.

## Contract choices

1. **Reuse, not a parallel store (msgs 1163, 1170).** Act records are ordinary `supervisor_journal.records` in the existing `(root, claimKey)` stream. `ActsJournal` **is** a `DurableJournal` and appends through `_append_tx`, so reservations (`dispatch_intent` reserves `dispatch_outcome` + the fault reserve), required outcomes, idempotent record ids (`uuid5(ActId)`), seq + hash chain, epoch + same-session fencing, `CommitUnknown`/`confirm()` and corrupt-stream retention are the E2b-a rules unchanged. Its route A path mirrors `append()`: identical resend is a no-op, a lost reply is `CommitUnknown` and the adapter is broken until `reacquire()`, fault hooks fire, `_expected` advances only on a committed record, intents re-check the fence (msg 1179; `test_lost_commit_reply_then_confirm_and_idempotent_resend`).
2. **Bookkeeping is derived** from the validated records (no second source of truth). Only two diagnostic tables are new: `e2c_counters` (u32 saturating; guard: grow-only, no TRUNCATE, DELETE only in compaction) and `e2c_queue` (immutable). **Rows are bounded**: counters and queue entries exist only for known keys (dispatched ExecutionKeys, requested reviews) plus one shared `_unknown_key` bucket, so arbitrary inbound keys cannot grow them (msg 1180; `test_arbitrary_inbound_keys_share_one_bounded_counter_bucket`).
3. **Route A (msg 1166).** One READ COMMITTED transaction: `pg_advisory_xact_lock(hashtext('hekate-plan-project:'||project))` **first** (the lock every PlanStore writer takes: `PlanStore.cs` 132, 235, 346, 538; managed nodes are fenced against other writers by the schema triggers), then the PlanStore facts (node row, that attempt's events, dependency edges, `event_seq`, the claim receipt), then journal global → writer → stream, decide, one record or one counter, COMMIT. No lock cycle: PlanStore writers never take journal locks and E2b-a appends never take the project lock.
4. **C-B** is one REPEATABLE READ READ ONLY snapshot over the same PlanStore rows and the journal. The journal read runs in a savepoint: on any journal failure the response is the already-read PlanStore leaf with `evidence: unavailable` and `current: unknown`.
5. **Single-writer serialization (msgs 1181, 1185).** The E2b-a fence refuses a second live session for the writer **before intake** (`fence_busy`; not weakened). Within the one session, **every** entry point that uses it runs under one re-entrant lock: route A, the malformed counter, `append`/`transport`, `resolve`, `verify_fence`, and `open`/`reacquire`/`close`. So no caller can commit or roll back an in-flight route A transaction and release the project lock early (`test_other_same_session_callers_cannot_commit_an_in_flight_route_a`). Route A transactions are therefore serialized, so concurrent callers are serialized and the in-transaction CAS sees every earlier commit: two threads submitting competing rebinds from the same stale read gave exactly one `accepted` and one `stale_binding`. The sequential test is named and documented as sequential only.
6. **Receipt correlation and stream identity (msg 1180).** An act or dispatch key must name the stream's `rootId` (and, for a worker, `claimKey`); otherwise it is malformed. Dispatch checks the authoritative receipt (`outcome`, node, attempt, epoch, `executorRef`, `contentRevision`, `prereqDigest`, content digest) under the lock. Route B facts must be `route == "B"`; independent facts can never create an effective act.
7. **Review decision identity (msgs 1175/1176).** `decided` needs `acc_attempt_id`, `acc_attempt_epoch` and `acc_artifact_ref` equal to the exact key and `acc_content_revision == content_revision`. The review then ends, with `acceptanceValidity` reported `unverified` separately. Any other decision identity is `operator_classification` (not ended). Missing or unrepresentable PlanStore facts (`> 2^53−1`, bool, negative) give `current: unknown`.
8. **Content currency (msg 1170).** A worker act on an unpinned attempt or with `content_revision ≠ attempt_content_revision` is `stale` with reason `stale_content` (PlanRules.PinFailure); already accepted evidence is kept. The current prerequisite digest needs whole-graph PlanRules, so `prerequisites` is always reported `unverified`, never guessed.
9. **Encoding (msg 1170).** E2c streams use `E2C_BOUNDS` (per stream 256, global 4000 records / 8 MiB); E2a/E2b-a defaults are unchanged. The full `packageRef` JCS lives in its own `package_ref` record; `dispatch_intent` stores its SHA-256 and C-B joins them to echo the full key.

10. **Selector agreement (msg 1188).** C-B refuses (`selector_mismatch`) unless the resolved identity's `rootId`, `nodeId` (and, for a worker, `claimKey`) equal the requested stream and node, and the PlanStore row read is that node. This is checked before anything is derived, in both the full-key and the lookup forms (`test_c_b_refuses_a_selector_that_disagrees…`).
11. **runId registry (msgs 1189, 1190).** `e2c_runs` (primary key `run_id`, immutable) is inserted in the dispatch's route A transaction after the stream lock. A duplicate across streams is refused `run_id_reused` and the whole transaction rolls back (`test_run_id_is_unique_across_streams`). **This is bounded to retained identities:** the row is deleted with its stream on compaction or eviction, so lifetime uniqueness is not proved, and no tombstones were added.

## Findings for the lead

- **Content digest case.** PlanStore's structured-content digest is **uppercase** hex (`seam.CONTENT_DIGEST`); frozen 030 `H` is lowercase. E2c converts explicitly at the boundary (`content_digest_from_planstore`: validate uppercase, then lowercase) and never matches loosely. A production contract must choose one form.
- **Fixture clock.** As in E2b-a, `at` is the adapter's fake clock, not the database clock 030 §7 proposes. Equal and regressing `at` were tested deliberately.
- **E2a classifier.** Not extended: a stream that carries E2c kinds may classify `unclassified` → `operator_reconcile`.
- **Workspace Api build** includes unowned uncommitted `Program.cs`/`CodeService.cs` changes; only the clean-source gate result excludes them.

## Not claimed

- Any production schema, storage selection (028 OQ1), coherent-read contract, endpoint, service or migration.
- Any production identity, auth or default: `hkw1`, the `packageRef` forms, G = 64, V = 16, K = 3 and the windows are fixture constants.
- Any wake mechanism, real worker or harness launch, bridge interaction or ChatAgent change. Transport facts are recorded only as historical evidence.
- Prerequisite currency or acceptance validity (needs whole-graph PlanRules).
- Effect fencing (as E2b-a: append fencing only), concurrency across processes beyond the single-writer fence, and anything about the E2a crash-point classifier for E2c streams.
- **Lifetime** `runId` uniqueness across retention (only retained registry rows are unique; msg 1190).
- No commits were made.

## Follow-up (future scope, msg 1191; not implemented)

User design steering: work need not stay in one monolithic, compacted conversation; a new conversation can pull across what it needs. A follow-up design should cover:
- durable task and evidence continuity that does not depend on a conversation surviving (the journal and PlanStore already hold it);
- selective context handoff and retrieval into a new conversation, rather than replaying or compacting the old one;
- an explicit session/attempt **ownership transition** that preserves deadline and evidence anchors. 030 forbids a worker rebind inside an attempt (`workerSession` = `executorRef`). So a conversation rollover must be reconciled deliberately (for example release plus a new claim, or a reviewed rebind contract), and never by silently treating a new conversation as the same worker session.

## Independent lead verification

Codex-hekate independently verified the seven overlay hashes in checkpoint 1193 against the table above. The final review used committed `d0ed671` plus only that overlay, with the Api built from the isolated source copy. Unrelated workspace Api changes were excluded. The interpreter was the existing Windows fixture `.venv/Scripts/python.exe`, with `HEKATE_E1_CONTAINER_WORKSPACE=D:/Git/Hekate` and `pytest -q` in the isolated fixture directory.

**Result: 486 passed in 73.22 seconds**: the 484 repository tests plus two independent reviewer probes. Those probes previously failed against checkpoint 1183 and now pass:

- C-B refuses a target ExecutionKey paired with another node's PlanStore selector.
- A concurrent malformed intake cannot commit while another route A transaction is open on the same adapter.

Review copies and probes are retained under `.review_tmp/e2c-lead-review-lt625__6/` (original checkpoint and failing reproductions) and `.review_tmp/e2c-final-review-j5pz5fq4/` (final overlay and passing probes). The final harness completed cleanup, and the review process exited successfully. The owned local database container remains running. Plan 030 still matches SHA-256 `52ef6784afdd2a8cd5619e8c042d635a7a343bc7af611fe41b1f29ba8e43ab3a`.

The lead accepts this fixture checkpoint with the stated limits: retained-data run-ID uniqueness only, prerequisite/acceptance validity unverified, fixture clocks and identities, and no production activation. No commits were made. Conversation rollover is recorded above as subsequent design work, not as implemented behavior.
