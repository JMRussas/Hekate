# Plan 020 — Durable claims and attempt pins (increment 3b1): validation evidence

**Status: accepted by codex-hekate after independent source review and verification. Scope: increment 3b1 only.**

**Date:** 2026-10-06
**Host:** fenrir (Windows 11, Docker Desktop, PostgreSQL 16 + AGE 1.6.0 + pgvector in the owned `hekate-local` container)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate

Design and exact semantics: [019](019-durable-claims-and-pins.md), including its "Implementation notes" for the review conditions. Background: [018](018-claim-receipts-design.md), [016](016-attempt-provenance-audit.md).

## Results (implementer's runs)

| Suite | Command | Result |
|---|---|---|
| Pure rules | `dotnet test context-store/PlanContracts.Tests` | **172 / 172** (146 before 3b1; 26 additional cases, including the updated contract tests) |
| Live store (PostgreSQL + AGE) | `HEKATE_PLAN_LIVE_CONNSTR=… dotnet test context-store/PlanStore.LiveTests` | **48 / 48** (34 before; 14 new) |
| Api process (HTTP) | `pwsh scripts/local/PlanContractApi.Tests.ps1` | **59 / 59** (39 before; 20 new `K` checks), exit 0, database verified dropped |
| Builds | `dotnet build` of `CodeStoragePoc`, `Api`, both test projects | 0 errors |

codex-hekate independently verified **172 pure and 48 live tests** on committed HEAD plus only the owned implementation files, excluding unrelated working-tree changes. **59 HTTP checks** also passed from the workspace against a new disposable database on port 5106. No further production code fix was needed (msg 602). Peer read-only review found no replay blocker.

**Isolation:**
- Every live run used new `hekate_plan_live_*` / `hekate_plan_api_*` databases and dropped them. Afterwards 0 `hekate_plan_live_*` databases remained.
- Tests that need destructive schema changes (the 3a upgrade, and the unknown-enum corruption, which drops a CHECK) use their **own** fresh database, never the shared fixture.
- No production host, NSSM service, sisyphus, model or provider call, worker, Odin/gods code, auth, UI or launcher code was touched.

## Acceptance criteria and evidence

| Criterion (019 and review messages 561, 563, 564, 569, 572, 584, 586) | Evidence |
|---|---|
| Pins set on start and reopen, kept on finish, cleared by release and cancel | Pure `Start_sets_pins_finish_keeps_them_release_and_cancel_clear_them_reopen_renews`; live `Claim_starts_the_first_ready_leaf…`; HTTP `K node view shows pins` |
| Legacy NULL-pin attempt: finish fails closed (`stale_content`); release and exact replay allowed | Pure `Legacy_unpinned_attempt_fails_closed_on_finish…`; live 3a upgrade test |
| Finish order: NULL pins → `stale_content`, content drift → `stale_content`, prerequisite drift → `stale_prerequisites` (409) | Pure `Own_content_revision_during_the_attempt_gives_stale_content`, `Prerequisite_drift_rejects_finish…`; live `Stale_prerequisites_and_stale_content_reject_a_pinned_finish`; HTTP `K finish stale_prerequisites 409`, `K finish stale_content 409` |
| Strict drift, each case separately: predecessor content, work, artifact, acceptance; edge added or removed; inherited ancestor edge; per-edge gate; plan default gate; transitive; container descendant; container child added | Pure theory `Prerequisite_drift_rejects_finish_with_stale_prerequisites` (12 cases), each asserting a changed digest and a rejected finish |
| No invalidation from an unrelated sibling or subtree, predecessor bookkeeping (`StateRevision`, op keys), sibling order, or the leaf's own finish and decide | Pure `Unrelated_work_own_operations_and_bookkeeping_do_not_invalidate`, `Unrelated_state_operations_do_not_stale_acceptance` |
| Canonical digest: deterministic regardless of insertion order, lowercase hex, nodes sorted by id, container children sorted by id | Pure `Digest_is_deterministic_regardless_of_insertion_order` |
| The snapshot uses the FULL raw acceptance tuple (decider and evidence included) and never pin-aware acceptance (msgs 561 and 563) | Pure `Snapshot_records_the_full_raw_acceptance_tuple_not_derived_acceptance` (evidence or decider changes alter the digest; a predecessor whose own pins drifted is still recorded raw) |
| Pin-aware `EffectiveAcceptance` shares one `Index` memo with container and gate derivation (msg 564) | Code: `EffectiveAcceptanceOf(g, id, Index)` is used by `Container` and `GateFailure`; only the public wrapper builds a new `Index` |
| Acceptance pins: a new Accepted after drift gives `stale_content` / `stale_prerequisites`; `gates_not_satisfied` keeps precedence; Rejected allowed; legacy Done keeps its acceptance while a new Accepted fails closed; exact latest-key decision replay is `Unchanged` | Pure `New_accepted_after_own_content_drift…`, `New_accepted_after_prerequisite_drift_with_green_gates…`, `Legacy_unpinned_done_keeps_its_acceptance…`, `Exact_latest_key_decision_replay_is_unchanged_after_drift`, `Upstream_change_after_finish_blocks_acceptance` |
| Updated 2a tests (019 §1b) | Pure `Adding_a_dependency_stales_a_pinned_acceptance_even_when_the_new_gate_is_green`, `Upstream_change_after_start_is_reported_and_rejects_the_finish`, `Upstream_change_after_finish_blocks_acceptance` |
| `ValidateState`: pins need an attempt, are set or NULL as a pair, and are NULL on containers | Pure `Pins_require_an_attempt_both_pins_and_a_leaf`; live `Replay_and_read_fail_closed_on_invalid…` (a partial pair loads as invalid) |
| New error code `stale_prerequisites` is reachable | Pure `ErrorCodeCoverageTests` |
| Pins on audit events, pre-clear for release and cancel (msg 569) | Live `Full_attempt_history…` (pins on all 7 attempt events, NULL on `work_restored` / `content_revised`, no claim key on generic events); live `Stale_prerequisites…` (release event keeps its pins while the state clears them) |
| Claim: the first ready leaf in hierarchy order; receipt with content and prerequisite snapshots; `attempt_started` event with `claim_key` and the internal key `hekate-claim:<key>`; `event_seq` linked; projection reconciled | Live `Claim_starts_the_first_ready_leaf_and_records_pins_event_and_receipt`; HTTP `K claim 200 claimed T2`, `K prereq snapshot has named states`, `K content snapshot`, `K claim event carries key and pins` |
| Replay returns the original receipt with no writes (state, events, receipts, `event_seq`), after a finish, a release, and a claim on another node; `stillCurrent` is factual | Live `Replay_returns_the_original_receipt…` (full-row fingerprint compared before and after); HTTP `K GET same receipt`, `K replay same receipt`, `K receipt unchanged after drift` |
| Payload conflict gives 409 `operation_key_reused` (attempt, executor reference or actor differ) and writes nothing | Live `Payload_conflict_and_input_errors_write_nothing`; HTTP `K payload conflict 409` |
| `no_ready_work` is stable and needs a new key; `stillCurrent=false`, `current=null` | Live `No_ready_work_is_stable_and_needs_a_new_key`; HTTP `K no_ready_work 200` |
| Concurrency: different keys get different leaves or exactly one `no_ready_work`; the same key and payload claims once and every response returns the same receipt | Live `Concurrent_claims_with_different_keys…` (3 claims over 2 leaves, `seq` [1, 2]), `Concurrent_same_key_same_payload_claims_once…` (4 parallel requests) |
| AGE failure (claimed, and `no_ready_work`) and sequence overflow leave no receipt, event or state | Live `AGE_failure_and_seq_exhaustion_leave_no_receipt_event_or_state` |
| Receipts are append-only even for the store; INSERT without the flag is fenced | Live `Receipts_are_append_only_even_for_the_store` (UPDATE, DELETE, TRUNCATE and unflagged INSERT give HP409) |
| Reserved prefix: new generic operations rejected (transition, dependency, create plan), exact latest-key replay of the claim's own start stays `Unchanged`, refused again once no longer latest | Live `Reserved_claim_prefix_is_rejected…`; HTTP `K reserved operation key 400` |
| Claim key wire contract (msgs 561 and 572): `[A-Za-z0-9._~-]{1,128}`, not `.` or `..`; `/ % ? #`, `:` and over-long keys give 400 with no receipt; a key with every allowed punctuation round-trips POST and GET | Live `Payload_conflict_and_input_errors…`; HTTP `K invalid claim keys 400 invalid_input` (7 variants), `K GET same receipt` (key `x._~-123`), `K missing claimKey 400`, `K invalid executorRef 400` |
| GET 404 `claim_not_found` / `plan_not_found` | Live `Payload_conflict…`; HTTP `K GET missing claim 404`, `K GET missing plan 404` |
| `stillCurrent` compares the saved content digest with the CURRENT content (msg 563) | Live `Still_current_compares_the_saved_content_digest_not_only_the_revision`: a raw trusted repair of the value without a revision bump makes it false while the attempt is untouched |
| Replay and read fail closed without writing on an invalid graph, an unsupported contract version and an unknown stored enum; a new claim on such a plan is refused (`invalid_graph` / `unsupported_contract_version`) | Live `Replay_and_read_fail_closed_on_invalid_or_unsupported_plans_without_writing`, `Replay_and_read_fail_closed_on_an_unknown_stored_enum_without_writing` (own database) |
| Receipt prerequisite snapshot JSON uses named enums (msg 584) | HTTP `K prereq snapshot has named states` (`work = done`, `decision = accepted`, `gate = accepted`) |
| **Real schema upgrade from the frozen 3a shape** | Live `Real_schema_upgrade_from_the_frozen_3a_shape_adds_3b1_and_preserves_existing_state`, detailed below |
| Earlier 2b1 → current upgrade test still describes a true historical shape (msg 584) | Live `Real_schema_upgrade_from_the_2b1_shape…` now first removes the receipts table (and its triggers) and both pin columns, asserts they are absent, then expects 17 triggers and compares state rows minus the three post-2b1 columns |

### Real 3a → 3b1 schema-upgrade test

The test runs on a **separate, fresh, test-owned** database created **without** the current plan schema (`LiveDatabase.WithoutPlanSchema()`).

1. Apply `PlanStore.LiveTests/Fixtures/schema-3a.sql`: the exact plan-contract DDL from commit `31274bc`, extracted from git with its two interpolations substituted. It is never edited. Assert 14 guard triggers, no receipts table, and no pin or claim-key columns.
2. Insert 3a-era rows with the store flag set:
   - a root;
   - an in-progress attempt (`x1`, epoch 1, executor `run-1`) whose stored fingerprint is the frozen 3a literal `10:transition;6:worker;10:InProgress;2:x1;~;~;12:executor_ref;5:run-1;`;
   - its 3a `attempt_started` event (`event_seq` 1);
   - a Done + accepted legacy node.
3. Run `PlanStoreSchema.Ensure` twice. Then assert:
   - 17 guard triggers, the receipts table, and 5 new columns;
   - every state row and the event row are identical as canonical `jsonb` except for the new columns;
   - all new columns are NULL (no backfill) and there are 0 receipts.
4. Behaviour after the upgrade:
   - the exact 3a retry returns **Unchanged** with no new event;
   - the legacy acceptance is still effective `accepted`;
   - finishing the NULL-pin attempt is refused with `stale_content`;
   - releasing it is **Applied**, and its event has NULL pins;
   - a new claim then starts the node at epoch 2.

## Changes to existing tests (for review)

| Test | Change | Reason |
|---|---|---|
| `State_only_operations_do_not_stale_acceptance` | Replaced by `Adding_a_dependency_stales_a_pinned_acceptance…` plus `Unrelated_state_operations_do_not_stale_acceptance` | Intended strengthening (019 §1b) |
| `Upstream_change_after_start_is_reported_and_blocks_acceptance` | Split: the finish is now `stale_prerequisites`; a finish before the change still has acceptance blocked by `gates_not_satisfied` | Intended strengthening (019 §1b) |
| `Events_are_append_only_even_for_the_store` | A plain `TRUNCATE plan_attempt_events` is now refused by PostgreSQL with `0A000` (the new receipts FK) before any trigger runs; `TRUNCATE … CASCADE` is asserted to hit the fence (HP409) | The receipts FK. TRUNCATE is still blocked in both forms |
| Trigger counts (2 tests) | 14 → 17 | 3 receipt guard triggers |
| `LiveDatabase` | One public constructor (the xUnit fixture rule) plus a static `WithoutPlanSchema()` | Needed for the 3a fixture |

## Known limits (unchanged by 3b1)

- No leases, heartbeats, expiry or reclaim: a claimed attempt stays InProgress until an explicit release or cancel (3b2).
- No auth: the actor, attempt id, claim key and executor reference are opaque correlation values. A receipt and `stillCurrent` are **not** authority and do not make external effects safe (018 §1).
- Strict invalidation: any relevant upstream change, including a harmless re-acceptance, stales an in-flight attempt; the worker must release and claim again.
- `stillCurrent` and the fences detect drift along well-behaved paths only; a holder of full database credentials can bypass triggers.
- Legacy NULL-pin in-progress attempts are not migrated and cannot finish; they can be released and started again, or cancelled. Legacy completed work retains its existing acceptance, and can be explicitly reopened for a new pinned attempt.
- No executor adapter, worker activation or workspace isolation.
- **Wire limit for JavaScript consumers (msg 592):** Int64 counters (revisions, epochs, `seq`, `eventSeq`, `contentRevision`) are emitted as JSON numbers, which is true of the existing v1 contract as well. JavaScript `Number` loses precision above 2^53−1. A future TypeScript adapter MUST either parse losslessly (or move to a decimal-string wire contract) or reject unsafe integer counters before acting on them, and must never round. 3b1 deliberately makes no serializer change and activates no adapter.
