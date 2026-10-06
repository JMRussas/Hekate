# Plan 025 — Supervisor E1b (ChatAgent H1 interop): validation evidence

**Status: accepted by codex-hekate after independent clean-source validation and codex-chatagent consumer review (msg 843), 2026-10-06.** Test-only: no production code, no ChatAgent edits or installs, no provider or model, no worker activation.

**Date:** 2026-10-06
**Host:** fenrir (Windows 11; uv 0.11.19 with CPython 3.13.13; Docker Desktop with the owned `hekate-local` container, used for the default suite and the one live case only)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate

**Scope:** [023](023-coding-worker-adapter-proposal.md) §3.3a **E1b**, under GO msg 820 and review conditions in msgs 823, 829, 833, 840 and 841. It builds on E1a (`bf61588`, [024](024-supervisor-e1a-validation.md)).

**ChatAgent H1 under test:**
- commit `5255daacfc670a4919f61439eb12adcb6a401920`, at `D:\Git\ChatAgent`;
- runtime: Node **v24.21.0** at `D:\Git\ChatAgent\node_modules\.cache\worker-diagnosis\new24\node.exe`, validated against the repository's tracked `.node-version` (recipe 812). The PATH Node (v24.15.0) is **not** used.

## Results (implementer's runs)

| Suite | Command (from `scripts/local/supervisor_e1`) | Result |
|---|---|---|
| Default: E1a plus fault injection (no ChatAgent dependency) | `uv run pytest -q` | **113 / 113** |
| Pure H1 interop (no HTTP, no container) | `uv run pytest interop -q` | **31 / 31** |
| Live H1 case (container plus H1) | `uv run pytest interop_live -q` | **1 / 1** |

**How the default count reached 113:**
- E1a's 103 became 104 when `supplied_sha256` joined the correlation sweep.
- Three more correlation fields (system, fast and deep instructions) add 3, giving 107.
- The new `tests/test_supervisor_faults.py` adds 6.

**Isolation:**
- The default and live suites each used one new `hekate_plan_e1_*` database, dropped and verified (0 remain). The Api ran on 5108 and has exited. No `.run/` work folder remains.
- H1 calls run ChatAgent's own code with ChatAgent's own `node_modules`, read-only. Nothing was installed or edited in ChatAgent.
- The owned container is left **running** for root's independent validation (msg 841).

## Acceptance criteria and evidence

| Condition (msgs 820, 823, 829, 833, 840, 841) | Evidence |
|---|---|
| The actual `buildPlanTaskContext` is called through `node --import tsx` with cwd set to the ChatAgent checkout; options go in on stdin; there are no ChatAgent edits or installs | `e1/h1_bridge.mjs` (renders nothing itself) and `e1/h1_bridge.py` |
| Pinned source and runtime fail closed: HEAD `5255daa…`; the H1 module, `src/app`, `src/domain`, `package.json`, `package-lock.json`, `tsconfig.json` and `.node-version` tracked and clean; tsx present; Node `--version` equal to `.node-version` exactly | `verify_checkout`, `node_runtime`. The interop conftest verifies at session start, so a failure **errors** and never skips. `test_a_wrong_checkout_fails_explicitly` shows a wrong checkout raises `H1Unavailable`, and in the supervisor it becomes `uncertain:package_build` with no writes |
| The default suite stays runnable without the sibling repo | `testpaths = ["tests"]`; the interop suites live in separate `interop/` and `interop_live/` folders that must be selected explicitly |
| Declared fixture IDs and digests are preserved verbatim | `test_claimed_fixture_declared_ids_and_digests_are_preserved_verbatim` (every identity equal in value and type; content digest uppercase, prerequisite digest lowercase) |
| Replay and current provenance are preserved, with stable mandatory text | `test_replay_keeps_provenance_facts_while_the_mandatory_text_and_hashes_are_stable`: identical text and `suppliedSha256`; `replayed` false vs true kept; semantic identity equal |
| `suppliedSha256` is exact and computed independently; supplied hashes are distinct from Hekate's structured digests | `test_supplied_sha256_is_independently_exact_and_distinct_from_hekate_digests` (Python `hashlib` over the exact UTF-8 text; rule hashes recomputed; none equals a Hekate digest) |
| Typed provenance: H1 output is checked against the supervisor's own parse of the same bytes | `package_from`, plus `test_typed_provenance_check_rejects_a_mismatched_or_mislabelled_result` (bool epoch, lowercased content digest, flipped `replayed`, relabelled hash, wrong system instruction) |
| Stability; the random `snapshotId` is excluded | `test_two_builds_are_identical_except_the_random_snapshot_id` |
| System and role instructions are captured separately; only the task text is hashed | `test_system_and_role_instructions_are_captured_separately_from_the_hashed_text`: the context carries exactly one message equal to the text, no memory and no optional context; the markers are absent from the text |
| Instructions are bound in semantic identity and in correlation (msgs 840 and 841) | `test_changed_instructions_change_semantic_identity_but_not_the_task_text_hash` ×3; `test_a_result_for_different_supplied_context_never_finishes` ×4 (system, fast, deep, `supplied_sha256`). `WorkResult` echoes the instructions as actual strings; there is no new digest |
| Mandatory requirement, attributes and rules appear whole | `test_requirement_attributes_and_every_rule_appear_whole` (derived input; byte-length-framed blocks) |
| Bounded refusals without echo, and no silent truncation | `test_context_budget_overflow_is_a_bounded_typed_refusal_without_echo` (`CONTEXT_TOO_LARGE` with integer token counts); `test_invalid_inputs_are_typed_refusals_that_echo_nothing` ×5 (`PACKAGE_TOO_LARGE`, `INVALID_OPTIONS` ×2, `INVALID_RULES` ×2); refusal objects carry only kind and code |
| Safe integers | `test_unsafe_number_is_refused_by_h1_even_though_it_fits_int64` (`eventSeq` 9007199254740993 is valid Int64 for the envelope but H1 returns `INVALID_NUMBER`); `test_h1_number_refusal_stops_before_dispatch`; bridge output is parsed with `loads_exact` |
| Frozen and copy semantics where feasible | `test_result_is_frozen_and_unaffected_by_later_input_mutation` (deep-frozen source, assignment rejected, unchanged after input mutation); `test_each_rule_field_is_read_exactly_once` (getter probe: one read per field, no second-read value anywhere) |
| H1 refusals stop before dispatch | `test_h1_refusals_stop_before_dispatch` ×3 (`package_overflow:CONTEXT_TOO_LARGE`, `package_overflow:PACKAGE_TOO_LARGE`, `package_refused:INVALID_RULES`) |
| Supervisor with the H1 package (stub client, real fixture bytes) | `test_supervisor_with_h1_package_correlates_supplied_sha256_and_finishes` (no H1 field is written to PlanStore); `test_supplied_sha256_mismatch_never_finishes` |
| Fault handling (msg 829) | `tests/test_supervisor_faults.py`, covering each fault below. Each keeps its evidence, and no fault triggers a second write |
| One live case: real raw claim bytes → H1 → fake worker → finish | `interop_live/test_h1_live.py` (real Api response, requirement and rule text present, the content digest verbatim, runtime v24.21.0 recorded, node Done, events `attempt_started` and `attempt_finished`) |

The faults covered in `tests/test_supervisor_faults.py`:
- a claim reply lost after commit: the claim committed exactly once, and a later run is a replay;
- a malformed 5xx claim reply: nothing was sent;
- a finish reply lost after commit: the held finish is kept, and a guarded replay gives `unchanged`;
- a finish dropped before sending: `reconcile:finish_unconfirmed`, as distinct from `reconcile:intervening_mutation`;
- a failed precondition read;
- a worker that raises.

## Not claimed

- **Restart durability:** there is no launch journal; that is E2.
- **Process safety and cleanup of a real worker:** that is E3. The H1 bridge is a short-lived pure computation whose owned child is killed on timeout.
- **Provider or model use, worker activation, auth, leases.**
- **That H1's rendering is the right prompt for a real coding worker:** E1b proves contract interop, not prompt quality.

## Files

**New:**
- `scripts/local/supervisor_e1/e1/{h1_bridge.mjs,h1_bridge.py,h1_package.py}`
- `scripts/local/supervisor_e1/tests/{test_supervisor_faults.py,helpers.py}`
- `scripts/local/supervisor_e1/interop/{conftest.py,test_h1_interop.py}`
- `scripts/local/supervisor_e1/interop_live/{conftest.py,test_h1_live.py}`
- this document

**Edited:**
- `scripts/local/supervisor_e1/e1/supervisor.py`: `package_builder`, `PackageRefused`, fault capture with retained evidence, the replay-helper reasons, and the new `WorkResult` correlation fields.
- `scripts/local/supervisor_e1/tests/{conftest.py,test_supervisor_live.py,test_fixture_correlation.py}`: helpers moved to `helpers.py`; the crash case is now a captured fault; the correlation inventory is updated.
- `scripts/local/supervisor_e1/pyproject.toml`: `pythonpath` adds `tests`.
- `scripts/local/supervisor_e1/README.md`: the E1b section.
- `context-store/plans/023-coding-worker-adapter-proposal.md`: the E1b status in §3.3a.

## Independent acceptance (codex-hekate)

Copied the 22 owned harness/manifest files byte for byte into `D:/hekate-browser-review-mz_6czag`, which contains clean accepted Api sources. Ran all three suites sequentially with `uv run --locked pytest`: **113 default tests in 21.51 s, 31 pure H1 tests in 40.12 s, and one live H1 test in 7.93 s; zero skipped.** Explicitly selected the original workspace's owned container label, the clean ChatAgent source at `5255daacfc670a4919f61439eb12adcb6a401920`, and its validated Node v24.21.0 executable.

Independent checks confirmed tested sources matched the workspace, the Api process exited, the disposable databases were absent, and `.run` was removed. Source review and peer consumer review verified the exact runtime/source pin, separate instruction identity/correlation, task-text hash scope, safe-number refusals, and structured fault evidence without automatic retries. This accepts E1b and the test-supervisor fault follow-up only. It does not activate a provider, worker, deployment, journal or recovery service.

## Replay scope clarification (design review 026)

The E1 replay helper's view checks are a conservative preflight in the tested immediate-replay scenario. Matching work, attempt, artifact and `stateRevision == expected + 1` does not prove the same last operation key or actor; the API's compare-and-set remains the final authority and can refuse a replay. The passing test does not establish permanent transition-key receipts or restart reconciliation. A future durable reconciliation needs exact audit correlation within a coherent read, or reports `proof_missing` for operator review. See [026](026-launch-and-review-evidence-proposal.md), C7. This clarification changes no code or test result.
