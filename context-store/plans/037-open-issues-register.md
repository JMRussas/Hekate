# Plan 037 — Hekate open-issues register

**Status: the canonical, living open-issue register for Hekate (revision 2, 2026-10-07; authorized by the user via msg 1437).** It replaces revision 1 (msg 1439, untagged entries) with stable IDs. One entry per issue; IDs are never reused or renumbered. It is docs-only: it changes no code, schema, fixture or accepted frozen artifact. Bridge checkpoints cite these IDs.

**Linked from:** the root `CLAUDE.md` deep-dive table (msg 1446, option b). The frozen supervisor README is unchanged.

## Migration rule (msg 1446)

Issues will eventually become Hekate plan nodes. When that happens:
- each `HK-ISSUE-NNN` ID is preserved and linked to its node ID (a one-to-one mapping recorded here);
- PlanStore becomes authoritative for status and assignment;
- this Markdown file stays as explanation and evidence links only, never a second ledger.

No nodes are created and no separate database is used until that migration is decided.

## How to use it

- **ID:** `HK-ISSUE-NNN`, stable. Earlier informal names (H-1, F1, D-1, …) are kept in the Alias column.
- **Gate class:**
  - `pilot-blocker`: blocks the supervised local pilot (task → artifact → independent V&V → fix → commit → fresh-conversation handoff).
  - `unattended-blocker`: blocks running that loop without a human watching.
  - `deferred`: production hardening or a later decision.
- **Kind:** `defect` (wrong behaviour, a fact), `interop-risk` (a mismatch that could become a defect), `implementation-gap` (missing code for the pilot), or `decision` (an owner must choose).
- **Status:** `open` → `assigned` → `implemented` → `verified` → `closed`. An issue is `closed` only when its row records the fix revision/hash and the verification evidence named in its closure criteria.
- **Affected revision:** Hekate `d0ed671` plus the uncommitted, accepted E2c/E2d/E2e overlay. File hashes are given where the issue is in a specific file.
- **Evidence directory** (git-ignored, repo-local): `scripts/local/supervisor_e1/.run/evidence-e2e-host-numprobe/`, with its own `SHA256SUMS`.

## Index

| ID | Alias | Title | Kind | Gate | Owner / assignee | Status |
|---|---|---|---|---|---|---|
| HK-ISSUE-001 | H-1 | `strict_loads` lets `RecursionError` escape | defect | deferred | Hekate / unassigned (planned increment, msg 1435) | open |
| HK-ISSUE-002 | H-2 | `revalidate` raises untyped errors on a missing identity field | defect | deferred | Hekate / unassigned (same increment) | open |
| HK-ISSUE-003 | F1 (036) | `attemptId` length unchecked on the transition route | defect | deferred | PlanStore / unassigned | open |
| HK-ISSUE-004 | F2 (036) | `attemptId` length counted differently by Python and C# | interop-risk | deferred | PlanStore + journal / unassigned | open |
| HK-ISSUE-005 | — | A fix round cannot be dispatched after a reopen | implementation-gap | unattended-blocker | PlanStore + journal / unassigned (dry-run workaround in use) | open |
| HK-ISSUE-006 | B1/B2 dry run | Pilot driver (B1) and dry run of B2 | implementation-gap | pilot-blocker | Hekate / claude-hekate | closed (bounded dry-run scope only; root acceptance msg 1479) |
| HK-ISSUE-007 | B2 | Real worker launch adapter (B2) | implementation-gap | pilot-blocker | Hekate / claude-hekate | closed (test-scoped supervised local toy run only; root acceptance msg 1585) |
| HK-ISSUE-008 | D-7 | Whether a real worker's ACK/progress is worker-attested or supervisor-observed | decision | pilot-blocker | root lead | provisionally decided for the pilot only (msg 1484) |
| HK-ISSUE-009 | D-1, 028 OQ1 | Journal storage beyond disposable test databases (028 OQ1) | decision | unattended-blocker | root lead / user | open |
| HK-ISSUE-010 | D-2, 028 OQ2 | Operator acts and authorization before auth exists (028 OQ2) | decision | unattended-blocker | root lead / user | open |
| HK-ISSUE-011 | D-3..D-6, 028 OQ3–6 | Remaining production questions (028 OQ3–OQ6) | decision | deferred | root lead | open |
| HK-ISSUE-012 | — | A prior-attempt decision blocks the fix round's review | implementation-gap | pilot-blocker | journal derivations / claude-hekate | closed (plan 038 rev 2; root acceptance msg 1479) |
| HK-ISSUE-013 | — | A real run's run.json says `dryRun: true` | defect | deferred | Hekate / claude-hekate (export producer slice) | closed (future runs: host-declared executionKind; root acceptance msg 1602) |
| HK-ISSUE-014 | — | Operator task runner v0 for a real task spec (CA012 first) | implementation-gap | pilot-blocker | Hekate / claude-hekate (plan 040) | closed (bounded operator runner v0, final source `30279d8`; root acceptance msg 1731) |

## Entries

### HK-ISSUE-001 — `strict_loads` lets `RecursionError` escape
- **Observed (fact):** `e1/consumer.py` `strict_loads` only catches `ValueError`. For JSON nested about 997–999 levels or deeper (the exact depth depends on the call stack), `json.loads` raises `RecursionError`, which escapes as an untyped exception instead of `Refused("strict_json")`.
- **Impact:** a malformed delivery crashes the consumer instead of being refused. Only the fixture depth is proven (the deepest value in both accepted fixture bundles is 7); no universal bound on producer depth is claimed.
- **Affected:** `consumer.py` sha256 `30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e`. The file is accepted and frozen.
- **Repro / evidence:** `depth.py` and `depth2.py` in the evidence directory (array depth 999, object depth 997 under Python 3.13.13). Reported in msgs 1404 and 1413.
- **Next action:** the separate hardening increment (msgs 1407, 1416). Catch `RecursionError` and refuse `strict_json`, or add an explicit depth limit. Keep the frozen baseline's provenance.
- **Closure (V&V):** a regression test refuses a depth-2000 document as `strict_json`. The default suite stays green. The golden and supplement replays still pass, or are re-baselined with recorded provenance.

### HK-ISSUE-002 — `revalidate` raises untyped errors on a missing identity field
- **Observed (fact):** `consumer.review_identity(manifest)` indexes the five identity fields directly. A manifest identity missing one raises `KeyError`, and a `Fresh` missing an attribute raises `AttributeError`; neither is a typed refusal.
- **Impact:** a malformed input gives an untyped crash, not `fresh_mismatch`/`delivery_mismatch`. Valid producer manifests always carry the fields.
- **Affected:** `consumer.py` (hash as in 001).
- **Repro / evidence:** source read; msg 1413, item H-2.
- **Next action:** the same increment as 001.
- **Closure (V&V):** regression tests: a manifest identity missing `attemptId` gives a typed refusal, and so does a `Fresh` with a missing field.

### HK-ISSUE-003 — `attemptId` length unchecked on the transition route
- **Observed (fact):**
  - `PlanStore.ClaimAsync` (PlanStore.cs 332) caps `attemptId` at 256 UTF-16 units.
  - `PlanRules.Transition` (PlanRules.cs 344–345) only rejects null or whitespace. No length check was found on `POST /nodes/{id}/transition`.
  - The journal wire (`acts.parse_attempt`) refuses more than 256 code points.
- **Impact:** an attempt started through a transition can carry an `attemptId` that the journal refuses (`Malformed string`), so its execution cannot be journaled.
- **Affected:** `PlanRules.cs` sha256 `7ad27b57e9fe4375751b55f9a70f5a2044c15fbdc1293d5f14e118b8c443749a` and `PlanStore.cs` `ba0258ba7c542dc5151ecf98ab450f79ccae800784e20120e32f377450d10713`, both tracked and unmodified at `d0ed671`.
- **Repro / evidence:** source read; [036](036-reference-source-of-truth-map.md) §7 F1. A runtime repro is not yet recorded.
- **Next action:** the PlanStore owner decides the bound (1–256, as on the claim route). A body-size limit may also apply but was not checked.
- **Closure (V&V):** a PlanStore test shows a transition with a 257-unit `attemptId` is rejected and a 256-unit one accepted. Contract docs are updated.

### HK-ISSUE-004 — `attemptId` length counted differently by Python and C#
- **Observed (fact):** Python `len` counts code points and C# `.Length` counts UTF-16 units. A non-BMP character counts 1 in Python and 2 in C#.
- **Interoperability risk (not an observed failure):** an `attemptId` between the two limits passes the journal wire but can never come from the claim route, and the reverse holds for other bounded strings. No producer path in the accepted fixtures hits this; pilot `attemptId`s are ASCII.
- **Affected:** `acts.py` `parse_attempt` against `PlanStore.cs` 332.
- **Repro / evidence:** 036 §7 F2.
- **Next action:** choose one unit, preferably UTF-16 units to match PlanStore or UTF-8 bytes, for every shared string bound, and document it.
- **Closure (V&V):** a cross-language test gives the same verdict on both sides for: 129 astral characters (129 code points but 258 UTF-16 units, which shows the divergence; 128 astral passes both and cannot show it); 256 and 257 ASCII characters (the `attemptId` boundary); and 128 and 129 ASCII characters (the `claimKey` boundary).

### HK-ISSUE-005 — A fix round cannot be dispatched after a reopen
- **Observed (fact):**
  - `ActsJournal.dispatch` (acts_durable.py sha256 `5ad500d623e8e85f8a107e7846bde1ac810cf8ce3242fa9b3402c70dce684a44`) requires the claim receipt of `(rootId, claimKey)` to name the node's current attempt, otherwise `receipt_mismatch`.
  - PlanStore's reopen (`done → in_progress`, PlanRules.cs 344–358) starts a new attempt and epoch without a claim.
  - A rejected `done` node is not ready work (`Ready = Todo && no blockers`, PlanRules.cs 253), so it cannot be claimed again.
- **Impact:** a reject → fix round cannot run on the journaled path through reopen.
- **Workaround (dry run only, accepted by msg 1440):**
  - Path: `done → cancelled → todo → claim` with a new `claimKey`, which opens a new journal stream for the round.
  - Every transition used is legal.
  - Prior rounds' attempt, artifact, review and decision evidence stay inspectable.
  - An accepted node is never reset.
  - This is a workaround, **not** a reopen-via-claim capability.
- **Next action:** the PlanStore and journal owners decide whether a claim-based reopen is needed for unattended operation.
- **Closure (V&V):** either a reviewed reopen-via-claim contract with tests, or a recorded decision that the workaround is the supported path, with tests that link rounds explicitly.

### HK-ISSUE-006 — Pilot driver (B1) and dry run of B2
- **Observed:** no driver composes claim → dispatch → acts → finish → review → handoff → decide → fix round. The accepted modules are tested only in isolation.
- **Scope (msg 1435):**
  - new `e1/pilot.py` and `tests/test_pilot_dryrun.py`;
  - disposable harness database, simulated worker and reviewer;
  - explicit repo, base, workspace and config inputs;
  - bounded fix loop;
  - exact artifact/revision correlation;
  - an uncertain launch fails closed.
  - No frozen module changes.
- **Implemented (dry run):**
  - `e1/pilot.py` sha256 `9c4f41509be67209a4b2bf12d010c59035b506165c04a388e812ec39b6b29f83`;
  - `tests/test_pilot_dryrun.py` sha256 `01744a8123b19764d4ea0dc3bb7535bb45413453dc50fb7e3481d3e3fe7a68b4`;
  - results: 20 passed + 1 strict xfail (HK-ISSUE-012); the default suite gives 613 passed, 1 xfailed; frozen-module hashes are asserted unchanged.
  - The single-round accept, uncertain/failed launch, unbound artifact, max-rounds, stale prior-round decision and run-log cases are verified.
  - The reject → fix → accept case could not complete with the accepted primitives (HK-ISSUE-012).
  - **Plan 038 revision (not yet run):** the strict xfail is removed; reject → fix → accept and an older-accepted-decision test are live tests; `FROZEN` no longer pins `acts.py`/`evidence.py`, which move to `REVISED_038` (values pending real `sha256sum`). The hashes above are pre-038.
- **Update (038 rev 2):** with HK-ISSUE-012 implemented, `test_reject_then_fix_round_then_accept` passes with no xfail; the full default suite gives 667 passed. Current hashes are in 038 §3.
- **Closed (root acceptance, msg 1479), for the bounded DRY-RUN scope only.**
  - Accepted evidence: the 038 §3 hashes (pilot.py `51f3d442…9cf8`, test_pilot_dryrun.py `06bc50ff…89d9`); full default suite 667 passed with no xfail; golden and supplement replays PASS.
  - Not claimed: a real worker launch (HK-ISSUE-007), ACK attestation (HK-ISSUE-008), the claim-based reopen (HK-ISSUE-005), or any unattended loop. Those stay open.
- **Closure (V&V):**
  - The dry-run tests pass against a disposable database.
  - The tests cover: reject → fix (new `claimKey` and epoch) → accept; a stale prior-round decision rejected; maxRounds giving needs_operator; an uncertain launch failing closed; and the handoff delivery verified and revalidated by the accepted consumer.
  - The frozen-file hashes are unchanged.
  - The default suite is green.
  - Root review is accepted.

### HK-ISSUE-007 — Real worker launch adapter (B2)
- **Observed:** `e1/supervisor.py` (sha256 `697b0bd31694431c26ba1b202717ed5b43f79351cf9d875e637d3eb4eda8e5f0`) only has `FakeWorker`. The real Claude Code CLI launcher lives in the gods pipeline (`Odin/gods/handlers/hermes_async.py`, sha256 `504fd0585e082b9fb5faeb0d31465d0b2dd60478aa1b9dbd49fa390fa9f5f5d4`) and is not wired to PlanStore or the journal.
- **Dependencies:** 006 (the driver seam), 008 (attestation), and launch parameters (repo/worktree, base, model, budget, time cap, kill rule) from the root lead.
- **Closure (V&V):** one supervised real run in a worktree records launch evidence, ACK/progress, the artifact SHA and the finish, all correlated, and passes the 006 suite with the real adapter swapped in.
- **Implemented (root GO msg 1484; the real model run NOT done):**
  - `e1/cli_worker.py` sha256 `e62c94ab1df6e49c8e02df4322c5863d06ae85788c3472db3be7c66d89ff380e`;
  - the `e1/pilot.py` hook (WorkOrder journal/act/delivered callbacks; WorkReport reason/attested; independent artifact re-verification; `no_worker_ack` stop) sha256 `95e8c0d826d8d36f0e317895256a6a076abe238ad5792e7ec01ba5fb6ac92e6b`;
  - `tests/fake_cli.py` sha256 `1d16fd99198b46a29c402837d4313f9cf5ff8e5fa055b07163fc03e1986abb6a`;
  - `tests/test_cli_worker.py` sha256 `3d0fe3c196e1de2067a8561b5765b7b5cbff0fe6a8de0f4882e6b84b49477f7a`.
- **What it does:**
  - config is validated before any git call or spawn; there is no shell and no skip-permissions; the MCP config is empty and strict; budget, turns and timeouts are explicit; tools are an allowlist plus Bash only for a configured test command;
  - an owned detached worktree;
  - `launch_intent` before spawn; `launched` is supervisor-observed, never an ACK;
  - bounded stdout (line, count and byte caps, a finite queue drained after a kill) and bounded stderr (a kept prefix plus a full digest and byte count);
  - a tree kill with a verified exit;
  - the supervisor commits; parent == base, the worker must not move HEAD, and the diff must be non-empty with no submodule;
  - a create-only run-owned ref;
  - explicit operator cleanup that refuses dirty or unknown worktrees and keeps the ref unless asked.
- **V&V:** 35 adapter tests pass, including the fake-child tree-kill proof and the pilot end to end through the adapter. The full default suite gives 702 passed; both replays PASS.
- **Scoped limitation (root, msg 1496):**
  - `taskkill /F /T` (and `killpg` on POSIX) is proven only for the tested case: a **live parent** with live descendants, where the fake's child dies.
  - It is **not** the same guarantee when the parent has **already exited** while descendants survive (for example, a worker that backgrounds a process and exits 0). Windows cannot walk a tree from a dead parent; `tree_kill` returns True for an already-exited parent.
  - Such survivors are **UNOBSERVABLE in the current scope**. The adapter neither detects nor contains them, and nothing makes that case automatically `unknown` (correction, msg 1501).
  - Containment (for example, a Windows Job Object over the whole tree, or a process-group check on POSIX) does not exist. This residual risk is accepted for the pilot only and must be reviewed before any unattended use.
- **CLOSED (root acceptance, msg 1585), for a test-scoped, supervised, local toy run only.** One authorized real run (root GO msg 1575, report msg 1580):
  - Source: commit `2fc9a1256617510958a064d4b5b8f2d4ee63acb9`, run from the clean detached worktree `D:\Git\Hekate\.worktrees\hk007-2fc9a125`.
  - Executable: `claude.exe` 2.1.285, sha256 `121fc815…697e`.
  - The run: requested model `sonnet`; CLI-reported `claude-sonnet-5-5` (not authenticated). Exit 0, exactly one `success` result, drained, no kill.
  - Acts: worker_ack and worker_progress, actSeq 1/2, same exec `956a97a8…`, worker-authored and accepted via intake.
  - Artifact `5774382537b03a02835ae324435698685d013fa8`, parent = base `934c45c144bccc31b2d0730c9a8b6a112eb86e11` (the task repo's main HEAD), retained ref `refs/hekate-pilot/4bf4cbb5022f/r1`. The diff is exactly `calc.py` 100644→100644, `raise NotImplementedError` → `return a + b`.
  - Independent verifier (verify-r1): 2 tests pass, tree clean; the root independently re-ran both tests there (PASS).
  - Evidence (retained): evidence.json `a00e8d3e…3e1a`, run.json `7ec8872e…87ac`, console log `5043f519…8f5f`.
  - **Not claimed:** a real-model fix round; authenticated ACKs; cost (not captured). The dead-parent descendant limitation stands. The run.json label defect is HK-ISSUE-013.
- **Was open:** the real model run, which needs root review of concrete parameters. On Windows, `claude` from npm is a `.cmd` shim, so the real executable/argument quoting path must be checked before that run.
  - **Checked:** the shim only invokes a native `claude.exe`, which the adapter can run directly with no `.cmd` and no shell. The shim is at `D:\scoop\apps\nodejs-lts\current\bin\claude.cmd`; the executable is at `D:\scoop\apps\nodejs-lts\24.15.0\bin\node_modules\@anthropic-ai\claude-code\bin\claude.exe`, version 2.1.285, sha256 `121fc8151ed40bd9c144d68aa1cea23427803628ffab65e23da1cceda155697e`.
  - A parse-only probe with an EMPTY prompt (no API call) accepted the adapter's exact argv. `--max-turns` is hidden from `--help` but accepted, while an unknown flag is rejected.

### HK-ISSUE-008 — Whether a real worker's ACK/progress is worker-attested or supervisor-observed
- **Question:** a CLI worker does not post E2c acts itself. Either the supervisor observes start and stream checkpoints and records them (then they are not independent attestation), or the worker gets an act channel.
- **Owner:** root lead. **Closure:** a recorded decision, reflected in 007.
- **Provisional decision (root, msg 1484), for the PILOT ONLY:**
  - Worker-authored `HEKATE-ACT {...}` lines in ASSISTANT text become worker_ack/worker_progress.
  - The supervisor binds the exact ExecutionKey and requires the next sequence number.
  - These are labelled a claimed worker attestation, **not an authenticated remote identity**.
  - Process spawn is `launched`, never an ACK.
  - Markers in tool input or output, or in user turns, never count.
  - Implemented in `e1/cli_worker.py` (HK-ISSUE-007); a final decision is still needed beyond the pilot.

### HK-ISSUE-009 — Journal storage beyond disposable test databases (028 OQ1)
- **Question:** should the journal be coupled to the PlanStore database or independent of it?
- **Current state:** journal tables exist only in disposable test databases; the dry run needs nothing more.
- **Owner:** root lead / user. **Closure:** a recorded decision.

### HK-ISSUE-010 — Operator acts and authorization before auth exists (028 OQ2)
- **Question:** who may take operator acts (takeover, resolution), and how they are authorized before real auth exists.
- **Owner:** root lead / user. **Closure:** a recorded decision.

### HK-ISSUE-011 — Remaining production questions (028 OQ3–OQ6)
- **Questions:** operator queue surface, production bounds, a production coherent read (also 026 OQ6), and an external anchor for chain heads.
- **Owner:** root lead. **Closure:** a recorded decision per question; each may be split into its own ID.

### HK-ISSUE-012 — A prior-attempt decision blocks the fix round's review
- **Observed (fact; found by the 006 dry run):**
  - After round 1 is rejected, the node keeps that decision (`acceptance` names attempt epoch 1) through `done → cancelled → todo` and the round-2 claim/start. PlanRules.cs 355–377: start, cancel and restore never clear `Acceptance`.
  - After round 2 finishes, the accepted `acts.review_state` (acts.py sha256 `1d44e39c…80e5`, lines 303–317) sees `acc_decision` set for another attempt and returns `operator_classification`, not `candidate`.
  - So `ActsJournal.request_review` refuses: "not a current Done candidate for this exact key".
- **Impact:** with accepted primitives, no fix round can be reviewed, handed off or decided. The pilot's reject → fix → accept loop cannot complete. The dry-run driver stops as `needs_operator` / `prior_decision_blocks_review`, naming the prior decision.
- **Repro / evidence:**
  - pre-038: `tests/test_pilot_dryrun.py::test_fix_round_runs_until_the_prior_rejection_blocks_its_review` (since replaced) and the strict xfail `::test_reject_then_fix_round_then_accept` (pilot test sha256 `01744a81…68b4`, 613 passed + 1 xfailed).
- **Decision (root, 2026-10-07): option (b).**
  - Option (a), PlanStore clearing the decision, was rejected: plan 012 and the PlanStore test `Reopen_issues_a_new_epoch_and_makes_the_old_acceptance_historical` require the record to be kept as history.
  - Option (c) was not chosen.
  - PlanStore is unchanged.
- **Implemented:** [038](038-older-attempt-decision-review-candidacy.md).
  - A valid decision with a strictly older positive integer attempt epoch leaves the current attempt a review candidate.
  - This applies in `acts.review_state`, `recovery.snapshot_review` and `evidence.derive_review` (a new revision of three formerly frozen modules).
  - Malformed or future epochs, invalid decision values, and same-epoch content/artifact/id drift stay operator classification.
  - An older accepted decision is never `decided`.
  - V&V (038 §4b, tool output):
    - root rev 1: 665 passed, replays PASS;
    - rev 2, after the attempt-id guard from root review msg 1474 (038 §4a): `test_hk012_older_attempt.py` 52 passed; the full default suite 667 passed with no xfail; golden and supplement replays PASS.
    - Real hashes are recorded in 038 §3 and in `REVISED_038`.
- **Dependencies:** HK-ISSUE-005 stays open (fix rounds still use the dry-run-only workaround).
- **Closure (V&V):**
  - `test_reject_then_fix_round_then_accept` passes (the xfail marker is already removed).
  - The prior-round stale-decision test still refuses, and no accepted node is ever reset.
  - The `test_hk012_older_attempt.py` and older-accepted-decision tests pass.
  - The full default suite and the golden/supplement replays are green.
  - 038 §3 and `REVISED_038` hold the real hashes.
  - Then `verified`, and `closed` after root acceptance.
- **Closed (root acceptance, msg 1479).**
  - The root reviewed the guard and the source at the exact hash recovery.py `c1af35bd…9fc6`.
  - The root independently ran the 52 offline tests (pass), on top of its earlier full run of 665 and both replays; my final run of 667 passed and both replays passed.
  - Scope: the review-candidacy rule of 038 only. HK-ISSUE-005, 007, 008 and the production gates stay open.
  - Root final full run (msg 1484): 667 passed in 120.37 s, log `.run/hk012-root-final.log`.

### HK-ISSUE-013 — A real run's run.json says `dryRun: true`
- **Observed (fact):** `e1/pilot.py` `write_log` (line 480 at `2fc9a12`) hardcodes `"dryRun": True`. The HK-ISSUE-007 real trial's run.json (sha256 `7ec8872e817a4feea471bb52966f529470e0e07601a33b16319a478850fd87ac`) therefore says `dryRun: true` for a REAL `claude.exe` run. evidence.json in the same run is correct (params.executable, the adapter evidence and the journal).
- **Impact:** a reader of run.json alone would mistake a real-model run for a simulated one.
- **Historical evidence is NOT rewritten:** the trial's run.json and evidence.json stay byte-identical (root, msg 1585).
- **Next action (in the export producer slice, branch `feat/handoff-export-v0`):** label FUTURE runs with an explicit HOST-DECLARED `executionKind`: `simulated` (in-process dry worker), `fake-cli` (the adapter driving tests/fake_cli.py) or `claude-cli` (the real executable). It is never inferred from `report.attested`, which is also true for the fake CLI. Define `dryRun = (executionKind != "claude-cli")`; it makes no authentication claim.
- **Closure (V&V):** tests show the actual mode gives `executionKind: claude-cli, dryRun: false`; the default simulated mode gives `simulated, true`; the fake CLI gives `fake-cli, true`. The trial's historical files are unchanged. Cost capture stays optional/deferred.
- **CLOSED (root acceptance, msg 1602), for the truthful host-declared mode of FUTURE runs only.**
  - `e1/pilot.py`: `Pilot(execution_kind=…)` is one of `simulated` | `fake-cli` | `claude-cli`; anything else is refused (`config_execution_kind`). The default is `simulated`.
  - run.json records `executionKind` and `dryRun = executionKind != "claude-cli"`, never inferred from `report.attested`. No authentication claim is made.
  - `e1/pilot_real.py`: `run()` requires `execution_kind`; `main` declares `fake-cli` when `--exe-arg` is given, else `claude-cli`.
  - Verified unit cases: the default gives simulated/true; the fake CLI gives fake-cli/true; the parametrized derivation gives claude-cli/false; an invalid kind is refused. Producer freeze 2: independent review msg 1600, root's 40 focused tests msg 1602.
  - The historical HK-ISSUE-007 trial bytes (run.json `7ec8872e…`, evidence.json `a00e8d3e…`) are preserved unchanged.

### HK-ISSUE-014 — Operator task runner v0 for a real task spec (CA012 first)
- **Gap:** the supervised pilot ran only a disposable toy task. A real task needs a closed spec, an owned clone, pinned dependencies, a proven baseline and a spec-driven verifier (GO msg 1632; spec frozen at `b919504a…`, msgs 1636/1650).
- **Implemented:** plan 040.
  - Code: `e1/task_spec.py`, `e1/task_runner.py` (`plan` / `preflight` / `run` / `verify`), the optional `prepare` hook in `e1/cli_worker.py`, and `run_bounded(merge=False)` in `e1/pilot_real.py`.
  - Branch `feat/operator-task-runner`: `78e029b` → `2890849` → `f4f1e3c` → `40657a9` → `2e43016` → **`30279d83a5814a641e3ba06c1da67645a9fe0d3a`** (final).
  - Review fixes: F1–F5 (msgs 1659/1660); the 1653 requirements; the npm cache and config scoping (1675–1695); the long paths, git stderr and verifier-only re-check (1702–1726).
- **Real runs:**
  - **Preflight-1:** refused at `deps_install_failed`, because the operator's npm cache was stripped. Its evidence is preserved (`a0b21d60…`).
  - **Preflight-2:** passed, 15 cases with 14 failed (`9296e902…`); ChatAgent independently checked it, 25/25 (msg 1699).
  - **The one claude-cli pilot** `pilot-b9cdfd1dc5b0` (Sonnet): produced artifact `a7fd2ec…` (3 allowlisted files) and then **stopped before verification**, `needs_operator` / `review_uncertain` / `verify_worktree_failed`, because of Windows MAX_PATH.
  - **One verifier-only re-check** of that artifact under `30279d8` (msg 1729): **accepted**, `all_steps_pass`. Evidence: `reverify-1/verify-evidence.json`, sha256 `3c05d6cd87e9014f21a89682b91e51dfcfe187cbb5ec4fc6d7280d2dabb23331`.
- **Interpretation (msg 1731):**
  - The **original pilot remains `needs_operator`**, with no PlanStore decision; its raw review view was not preserved.
  - The **accepted result is the artifact re-verification** using the recorded binding. It is **not** an accepted original pilot, and not a replay of the original review.
- **Scope:**
  - Verifier acceptance covers the spec's steps (oracle + `tsc`) only.
  - Full-repo integration quality is separate: ChatAgent's integration commit fixes the route inventory, an HTTP test timeout and a Prettier wrap. Root reports ChatAgent's full suite at 2171 passed / 9 skipped, plus lint and docs checks.
- **Not claimed:** OS isolation; a real fix round; cost capture; integration of this branch (root's).
- **Closure (V&V):** all three listed criteria are met.
  1. Independent review of the frozen source: ChatAgent msgs 1671 and 1695; root's final review of `30279d8`, msg 1726.
  2. Root acceptance: msg 1731.
  3. A root-GO'd real CA012 preflight with recorded evidence: preflight-2.
  - Full default suite on `30279d8`: 933 passed, 1 skipped.
- **CLOSED (root acceptance, msg 1731)** for the bounded operator runner v0 only.

## External references (owned elsewhere; not HK issues)

- **ChatAgent L1** → **CA-ISSUE-001** in ChatAgent's register, `D:/Git/ChatAgent/docs/open-issues.md` (authoritative state lives there, not here). Summary: a detached `ArrayBuffer` field escapes the delivery validator as a `TypeError`. Found by claude-hekate's targeted review (msg 1430).

## Corrections log

- **2026-10-07 (msg 1435):** the pilot-path message (1432) listed "user approval per commit/merge" as a gate. It was a recommendation with no source. Local commits are authorized and the root lead is the integration owner.
