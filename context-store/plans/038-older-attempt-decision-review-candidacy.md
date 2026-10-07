# Plan 038 — Older-attempt decisions leave a new attempt reviewable (HK-ISSUE-012 amendment)

**Status: ACCEPTED by the root (msg 1479; HK-ISSUE-012 closed for this scope). Revision 2, 2026-10-07: hashes and V&V evidence filled from tool output; attempt-id guard added in `recovery.snapshot_review` after root review msg 1474.** This note records a root decision to revise three formerly frozen supervisor modules. It **supersedes** one classification rule in [026 §7](026-launch-and-review-evidence-proposal.md#7-review-candidates-vs-accept-eligibility-derived-no-second-ledger) and the matching sentence in [030](030-worker-ack-progress-review-boundary.md) (source facts, line 41).

It does **not** change [012](012-plan-node-contracts-v1.md), PlanStore, any schema, the golden or byte-compat fixture bundles, `consumer.py` or `handoff.py`. The historical acceptance results in 031, 033 and 035 stay as recorded: they tested the pre-038 code, and their hashes are not rewritten.

**Task:** root decision on HK-ISSUE-012 (option B), following claude-hekate's read-only analysis.
**Register:** [037](037-open-issues-register.md) HK-ISSUE-012.

## 1. Problem

Plan 012 keeps a decision record as history when a node gets a new attempt, and reports it as `Stale` (012 "Effective acceptance"). The accepted PlanStore test `Reopen_issues_a_new_epoch_and_makes_the_old_acceptance_historical` asserts this. 026 §7 classified every `stale` Done leaf as operator classification. So after round 1 was rejected and round 2 finished, the round-2 attempt could never become a review candidate. `request_review`, the E2d handoff and the E2e revalidation all refused it, and reject → fix → accept could not complete.

## 2. Rule (supersedes 026 §7 for this case)

A review key that matches the node's current Done attempt (id, epoch, artifact) is a **review candidate** when the node's recorded decision is **valid** and belongs to a **strictly older positive integer attempt epoch**:

- `acc_decision ∈ {accepted, rejected}`;
- `acc_attempt_epoch` is an int (not a bool), `≥ 1`, and `<` the current attempt epoch.

Why it is safe, from PlanStore facts (no change needed there):
- epochs are issued `+1` on every start or reopen and never reset (012 "Attempt fencing");
- `ValidateState` refuses a decision epoch above the current one, and a same-epoch decision that names another attempt id (PlanRules.cs 153, 159–160).

So an older epoch is a different attempt, whatever its attempt id. Attempt ids may be reused, and the epoch is the proof.

Everything else is unchanged and fails closed:

| Decision on the node | Class |
|---|---|
| none | candidate (unchanged) |
| valid, strictly older epoch, rejected **or accepted** | **candidate** (new). An older accepted decision is never `decided` and never revives approval |
| valid, same epoch, matching id, artifact and content | decided (unchanged) |
| same epoch with content, artifact or attempt-id drift | operator classification (unchanged) |
| epoch missing, bool, non-int, `< 1`, or in the future; unknown decision value | operator classification (fail closed; an unknown value is now also never `decided`) |

Unchanged: the record stays history in PlanStore, the audit `decision_recorded` events are untouched, gates and pins are as before, and accept-eligibility is still derived separately. An old approval does not satisfy a dependent Accepted gate (`predecessor_acceptance_stale`) until the new attempt is itself accepted.

## 3. Changed files

| File | Change | Pre-038 hash (provenance) | Post-038 hash |
|---|---|---|---|
| `scripts/local/supervisor_e1/e1/evidence.py` | new `DECISIONS`, `older_attempt_decision()`; `derive_review` treats a `stale` view whose `acceptance.attemptEpoch` is provably older as a candidate | `f8affab90705d6229e12e2bb990d6ee57fba3244c36a0be46f82ffd79f77cbde` (031) | `db8b7a791fd07a73b42b9a47613f3fb9c5689020c8cfedda37a4ddd3af0f74f7` |
| `scripts/local/supervisor_e1/e1/acts.py` | `review_state`: older-epoch rule; `decided` also requires a valid decision value | `1d44e39c015be3d259cd359e284e9e6b999d369f58f80e4b4a57d698ed8e80e5` (031, 035) | `ffa11dd506b5195fc0db6e813ec74bb9a97ef91d6e7027082436b8989b6bf97b` |
| `scripts/local/supervisor_e1/e1/recovery.py` | `snapshot_review`: older-epoch rule; an invalid decision value is operator classification; **rev 2:** a same-epoch decision naming another attempt id is operator classification (agrees with `acts.review_state`; see §4a) | last recorded `927fc646d94c176c610433c9d1587ea81b8393cfbea183765287f2776a656de1` (029); the pre-038 working-tree hash was not recomputed. Rev 1 (root V&V): `f3424986ddde77ec8fad2d99f8aa79bb208f77de82682f6a0b213d1a96153f91` | `c1af35bd00ddc5d8f28d30fa228862db7c79a0822e68d2099c31faf0be849fc6` |
| `scripts/local/supervisor_e1/e1/pilot.py` | docstring and comment only (gap fixed; the fail-closed stop kept as a diagnostic) | `9c4f41509be67209a4b2bf12d010c59035b506165c04a388e812ec39b6b29f83` | `51f3d442128d9e2b8caf678a8f8442edf3a7bdfb1bb19b52e96d659d6f629cf8` |
| `scripts/local/supervisor_e1/tests/test_hk012_older_attempt.py` (new) | offline model and agreement tests; **rev 2:** + the same-epoch other-attempt-id case | — | `5acbe89ab6df16ad0cf74daf3a2a499aa609ba5d2d74e11126468139f957a252` |
| `scripts/local/supervisor_e1/tests/test_pilot_dryrun.py` | strict xfail removed; full reject → fix → accept and older-accepted-decision live tests; `FROZEN` no longer pins `acts.py`/`evidence.py`, and `REVISED_038` pins the three revised modules | `01744a8123b19764d4ea0dc3bb7535bb45413453dc50fb7e3481d3e3fe7a68b4` | `06bc50ff940b9f7b25df1c98540d91c5978e3cd987d33b5d95d25bf66f9f89d9` |

**Provenance:** the revision-1 implementing session had no shell, so the root computed the rev-1 hashes and filled `REVISED_038` from tool output. Every post-038 value above was produced by `sha256sum` after the revision-2 change (claude-hekate, msg 1474 hand-back), and `REVISED_038` now pins recovery.py `c1af35bd…9fc6`.

## 4. Tests

- `tests/test_hk012_older_attempt.py` (offline):
  - the guard (old and future epochs, None, bool, 0, −1, string, float, invalid decision values);
  - `review_state` matrix: an older rejected or accepted decision, an older decision with the same artifact, the same attempt id reused across epochs, a new attempt id, every malformed epoch, invalid values with an older epoch and with an exact match, same-epoch content (accepted and rejected), artifact and id drift, exact decided, none, and a moot round-1 key;
  - agreement of `acts.review_state`, `recovery.snapshot_review` and `evidence.derive_review` over every PlanStore-reachable state;
  - a `stale` view without a provable older record stays operator classification;
  - round 2: `request_review` + ACK, then E2d `build` (mandatory proof) + `decide_commit`, for older rejected and accepted decisions;
  - round 2 becomes `decided` only by its own decision.
- `tests/test_pilot_dryrun.py` (live, disposable harness DB):
  - `test_reject_then_fix_round_then_accept` (no longer xfail) checks: a new claimKey and epoch; the cross-round link; round-2 handoff + Fresh candidate + decision; effective acceptance `accepted`; and both `decision_recorded` events kept;
  - `test_an_older_accepted_decision_is_never_revived`: a dependent Accepted gate holds after round 1, then after reopen + finish the record is kept, `effectiveAcceptance = stale`, the successor is blocked with `predecessor_acceptance_stale`, and the three derivations say `candidate`;
  - the stale prior-round decision is still refused and nothing is reset.
- Unchanged and still expected to pass: `test_e2c_model.py::test_a_decision_against_another_identity_is_operator_classification_not_ended` (its four cases are same-epoch or future-epoch), the E2a/E2b reopen-makes-moot tests, and the 026 §7 `stale` → operator-classification case in `test_e2a_model.py` (no acceptance record ⇒ fail closed).

### 4a. Root review question (msg 1474): the attempt id in `snapshot_review`

- **Facts:**
  - The authoritative projection carries `acc_attempt_id` (`row_to_json(plan_node_state)`).
  - PlanStore's `RawAcceptance` (PlanRules.cs 185–192), which `snapshot_review` mirrors, compares content revision, artifact and epoch, **not** the id.
  - The id is enforced instead by `ValidateState` (PlanRules.cs 159): a same-epoch decision naming another attempt id is an **invalid state**. PlanStore never writes one (Decide records the current attempt id) and refuses to load one.
- **Gap:**
  - That state is unreachable through PlanStore, so there is no semantic gap for valid states.
  - A raw row showing it (corruption, a trigger bypass) was classified `decided_unverified` by recovery but `operator_classification` by `acts.review_state`. The two raw-row derivations disagreed, and recovery's answer did not fail closed.
- **Change:** a bounded guard (`acc_attempt_id != attempt_id` → operator classification) plus the regression `test_same_epoch_other_attempt_id_fails_closed_in_acts_and_recovery` (accepted and rejected). `evidence.derive_review` is not applicable, because PlanStore produces no plan view for an invalid snapshot. PlanStore is unchanged.

### 4b. V&V evidence (tool output)

- **Root, rev 1:** 50 new offline tests pass; the full default suite gives 665 passed in 127.31 s, no xfail; the golden and supplement replays pass. Log: `scripts/local/supervisor_e1/.run/hk012-root-full.log` (msg 1474).
- **claude-hekate, rev 2 (after the 4a guard):**
  - `tests/test_hk012_older_attempt.py`: 52 passed;
  - full default suite (`uv run pytest -q`): **667 passed** in 116.21 s, no xfail;
  - `fixtures/e2e-consumer-v0/replay.py`: REPLAY PASS; `fixtures/e2e-byte-compat-v0/replay.py`: REPLAY PASS.
- **Root, final rev 2 (msg 1484):** full default suite 667 passed in 120.37 s (log `scripts/local/supervisor_e1/.run/hk012-root-final.log`); the 52 focused tests were run independently; and the source was hash-reviewed.

**Required V&V (root):**
1. The full default suite (`uv run pytest -q` in `scripts/local/supervisor_e1`).
2. The golden and supplement replays (`uv run python fixtures/e2e-consumer-v0/replay.py` and `fixtures/e2e-byte-compat-v0/replay.py`). Bundle bytes, `consumer.py` and `handoff.py` are unchanged; the replays use only `acts.canonical`, which is unchanged.
3. Record the hashes above.

The pinned H1 interop is not required: the H1 seam is unchanged.

## 5. Not changed / still open

- PlanStore, 012, the PlanStore tests and the API are unchanged.
- **HK-ISSUE-005** stays open. Fix rounds still dispatch through the dry-run-only cancel → todo → new-claim workaround.
- ChatAgent `devCoordination.stateOf` still maps a Done + `stale` node to `stale`. The matching host change (older-epoch acceptance → `review_pending`) is ChatAgent-owned and needs its own CA issue.
