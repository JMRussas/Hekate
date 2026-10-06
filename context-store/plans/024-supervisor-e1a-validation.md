# Plan 024 — Supervisor E1a: validation evidence

**Status: accepted by codex-hekate after independent clean-source validation on 2026-10-06.** Test-only experiment: no production code, no process, CLI or model launch, no worker activation.

**Date:** 2026-10-06
**Host:** fenrir (Windows 11; uv 0.11.19 with CPython 3.13.13; .NET for the Api build; Docker Desktop, PostgreSQL 16 + AGE in the owned `hekate-local` container)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate
**Scope:** [023](023-coding-worker-adapter-proposal.md) §3.3a, **E1a** only, under GO msg 778 and review conditions in msgs 781, 787, 789, 792 and 801. Base: `68bab95`. E1b (ChatAgent H1 package fields, rendering, `packageDigest`, case 16 interop) is pending H1's accepted checkpoint.

## Results (implementer's run)

| Suite | Command | Result |
|---|---|---|
| Offline fixture and unit | `cd scripts/local/supervisor_e1; uv run pytest tests/test_fixture_correlation.py` | **57 / 57** |
| Live, real API | `uv run pytest tests/test_supervisor_live.py` | **46 / 46** |
| Full | `uv run pytest -q` | **103 / 103** (19 s) |

**Isolation:**
- One new `hekate_plan_e1_*` database per run, in the owned container (label-verified, loopback). It was dropped and verified: afterwards there were 0 `hekate_plan_e1_*` databases.
- The Api ran on `127.0.0.1:5108` from a private build folder and was killed at teardown.
- The `.run/` work folder was removed after a clean run. On a failing run it is kept for diagnosis, which was observed once and then removed.
- No production code, launcher, existing test, package manifest, ChatAgent or Odin file was touched, and the unknown dirty work was preserved.

## Acceptance criteria and evidence

| 023 case / condition | Evidence (tests) |
|---|---|
| 1 Happy path with a compare-and-set on the current revision; the verifier decides separately | `test_happy_path_finishes_with_cas_and_verifier_decides_separately`: `attempt_started` (claim key) then `attempt_finished` (`supervisor:<key>:finish`); `expectedStateRevision` = the revision read just before; the decision actor is `e1-verifier` |
| No ready work: nothing dispatched | `test_no_ready_work_dispatches_nothing` |
| 2 Upstream drift means no finish | `test_upstream_drift_during_the_run_is_needs_operator_without_finish` (`not_still_current`; only `attempt_started` exists) |
| 3 Own content revised means no finish; a forced finish is `stale_content` | `test_own_content_revision_is_needs_operator_and_a_forced_finish_is_stale_content` |
| 4 Each correlation field mismatched in turn (10 fields) | `test_each_correlation_field_mismatch_is_rejected_before_any_write[...]` ×10 |
| 5 Non-success shapes, whatever the prose ("SUCCESS!") | `test_non_success_shapes_never_finish_whatever_the_prose[...]` ×6 |
| Runtime type validation (msg 789): bool or float epoch, `exit_code` 0.0/False/None/"0", `timed_out`/`killed` None/0/"false", blank or non-string artifact, string structured result, null claim key, blank run id or executor reference, null prose | `test_malformed_result_types_never_finish[...]` ×20; correlation compares type as well as value (`True` never equals epoch 1) |
| 6 Replayed claim: no dispatch, no write; an explicit `uncertain` state is the same; only an explicit operator release writes | `test_replayed_claim_never_dispatches_and_only_an_explicit_operator_release_writes`: the database snapshot is unchanged across the replay; the release needs an operator and reason; the release event key is `supervisor:<key>:release` with pins captured before clear |
| Fresh but not-current claim is never dispatched (msg 789) | `test_a_fresh_claim_that_is_not_current_is_never_dispatched` (derived input through a stub client) |
| 7 Deterministic package identity; fresh run ids | `test_package_identity_is_deterministic_and_run_ids_are_fresh` |
| 8 Bounded capabilities and a pure seam | `test_supervisor_client_has_no_setup_or_verifier_capabilities` (no create/add/decide/revise; only done/todo transitions; loopback only); `test_seam_modules_are_pure_by_import_list` (AST: `seam.py` and `exact.py` import only `re`, `json`, `dataclasses`, `typing`; no `open`/`eval`/`exec`) |
| 9 Digest forms and labels | Offline: `contentDigest` uppercase-64 and `prereqDigest` lowercase-64 enforced with `fullmatch`; case-swapped digests rejected; `prereqSnapshot.digest` must equal `prereqDigest` |
| 10 Exact counters | `test_large_integers_stay_exact_and_counters_are_bounded`; live events carry `int` `seq` and epochs; float and exponent tokens rejected, never rounded |
| Raw integer tokens vs semantic counters (msg 801) | Every integer token must fit Int64 at parse time (`integer_out_of_range`), including nested and duplicate-shadowed tokens. Duplicate keys are rejected (`duplicate_key`). Semantic counter rules (epoch ≥ 1, seq ≥ 1, revision ≥ 1, no bools) are separate (`unexpected_shape`). See the float/duplicate parametrized cases and the derived-document table |
| 11 Pinned-content mismatch | Correlation cases for `content_digest` and `prereq_digest`; a snapshot-digest mismatch in the receipt is `correlation_mismatch` |
| 13 Race between pre-check and finish | `test_race_between_precheck_and_finish_is_rejected_by_planstore_and_not_retried` (409 `stale_prerequisites`, exactly one attempt) |
| 14 Stale compare-and-set | `test_stale_expected_state_revision_is_409_without_retry` |
| 15 Last-operation idempotency | `test_finish_idempotency_is_last_operation_only`: an immediate guarded replay gives `unchanged`. After an intervening verifier decision the supervisor **sends nothing** (`reconcile:intervening_mutation`). Separate raw-API fixture calls show `operation_key_reused` and `stale_revision`; the database is unchanged |
| 16 (fixture part) Real raw claim fixtures | `test_claimed_fixture_validates_whole_document`, `test_replayed_fixture_correlates_with_the_claimed_one`, `test_no_ready_work_fixture_yields_no_package`, `test_opaque_package_is_explicitly_opaque_and_verbatim` |
| Whole-envelope validation (msgs 781 and 792) | Over 30 derived invalid documents fail closed: version, outcome, digest case, snapshot digest, bool/zero/negative/overflow counters, a lying `stillCurrent`, a missing field, a dot-segment key, a trailing newline (`fullmatch`), content-snapshot types and keys, chain as a string or list of strings, chain order, nested node epoch/revision/work/kind, acceptance decision and epoch type, pinned digest case, missing acceptance, declared gate and dangling predecessor, `no_ready_work` carrying fields or `stillCurrent`. `test_snapshot_equality_is_semantic_not_property_order` covers order-insensitive equality |
| Harness cleanup (msg 787) | Each step is independent and errors are aggregated without raising; the original exception is kept, with cleanup notes attached. Generated work is deleted only after a confirmed Api exit and database drop. `uuid4` project ids. An explicit validated container-workspace override for clean-checkout runs |

## Not claimed

- **Restart durability:** E1a has no launch journal. The replay and `uncertain` cases test policy only (E2).
- **Process safety, cleanup or cancellation:** nothing is launched (E3).
- **H1 interop, rendering, `packageDigest` and `suppliedTaskTextSha256`:** E1b, after H1 is accepted.
- **Auth, leases, heartbeats, worker activation:** none.

## Files (all new unless marked)

- `scripts/local/supervisor_e1/`:
  - `pyproject.toml`, `uv.lock`, `.gitignore` (`.venv/`, `.pytest_cache/`, `__pycache__/`, `.run/`), `README.md`;
  - `e1/{__init__,exact,seam,wire,supervisor,harness}.py`;
  - `tests/{conftest,test_fixture_correlation,test_supervisor_live}.py`.

  This differs from the msg 780 inventory in two ways: `e1/exact.py` was split out so the seam's purity can be proven by its imports, and a local `.gitignore` was added.
- `context-store/plans/023-coding-worker-adapter-proposal.md` (**edited**): §3.3a E1a/E1b split and §3.4 exit criteria.
- this document.

## Independent acceptance (codex-hekate)

Ran `uv run --locked pytest -q` with Windows uv 0.11.19 / CPython 3.13.13 in `D:/hekate-browser-review-mz_6czag/scripts/local/supervisor_e1`, over clean Api sources from accepted `bb2af8b`. The 13 owned harness/manifest files and four committed raw fixture/provenance files matched the source workspace byte for byte. An explicit validated `HEKATE_E1_CONTAINER_WORKSPACE=D:\Git\Hekate` selected the owned container while building isolated sources.

**103 passed, zero skipped, in 19.10 s:** 57 offline and 46 live. A fresh environment was created from `uv.lock`; no production dependency or source changed. The private Api exited, its disposable database was verified absent, and `.run` was removed. Independent source review confirmed the strict result types, full numeric parsing, receipt correlation, stale pin/CAS refusals, fresh-claim dispatch rule, and last-operation replay limit. This accepts E1a only; it does not supply H1 context rendering, process execution or durable restart evidence.
