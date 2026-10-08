# Plan 041 — reference consumer typed-refusal hardening (HK-ISSUE-001 + HK-ISSUE-002)

**Status: accepted and closed (2026-10-07; root msg 1754).** HK-ISSUE-001 and HK-ISSUE-002 are closed.
- **Source:** commit `7d62c68c6d873adca8dfa44a53eac415481ec8fd`, `e1/consumer.py` `aea15fa4…`.
- **Reviews:** ChatAgent Claude's independent review (msg 1752) accepted it with no blockers, after its own direct probes and its own `replay_revision` run. Root accepted it after reading the consumer diff and `replay_revision` (msg 1754).
- **Full default suite on these sources:** 991 passed, 1 skipped (the opt-in live npm check), log sha256 `8272d481…5810`.
- **Branch:** `fix/consumer-typed-refusals` from `722aa12`. Assignment: bridge msg 1741.
- **How it was done:** direct Claude-lead hardening work. It was **not** routed through the operator task runner (plan 040): that runner is Node-only and does not execute Python repo tasks.
- **No model run.**

## 1. The defects (both in the accepted `e1/consumer.py`, sha256 `30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e`)

| Issue | Input | Before (reproduced on `30ca084a`) | After |
|---|---|---|---|
| HK-ISSUE-001 | JSON nested 2000 deep (array or object) | `RecursionError` escapes `strict_loads` | `Refused("strict_json", "<what>: nesting too deep")` |
| HK-ISSUE-002 | a manifest identity missing one of the AttemptKey fields or `artifactRef` | `KeyError` escapes `review_identity` | `Refused("delivery_mismatch", "manifest identity")`, **at `verify_delivery`** |
| HK-ISSUE-002 | a revalidation proof missing a `Fresh` field | `AttributeError` escapes `revalidate` | `Refused("fresh_mismatch", "…malformed: missing [...]")` |

## 2. The change (`e1/consumer.py` → sha256 `aea15fa421b5a857d9393dbbdff5073a977de5e28bf4a0854b35c0d1983475ed`)

- **`strict_loads`:** `RecursionError` from `json.loads`, **or** from the recursive `_scalars_ok` scan, becomes `strict_json`. A document can just fit the parser and still overflow the scan, so both are covered.
  - There is no new depth limit and no recursion policy (out of scope, msg 1741). The interpreter's own recursion limit stays the boundary; crossing it is now a typed refusal.
- **`review_identity`:** a missing field or a malformed path (`KeyError` / `TypeError`) becomes `delivery_mismatch` ("manifest identity").
- **`verify_delivery`:** now calls `review_identity` inside its shape check, so a delivery whose manifest lacks an identity field is refused **at verification**, before any revalidation, policy or H1 call.
- **`revalidate`:**
  - A proof missing any of `FRESH_FIELDS`, or with a non-list `pending`/`queue`, gives `fresh_mismatch`.
  - A `Verified` missing a bound field (identity, `task.packageRef`, `receipt.candidateDigest`/`recordId`, `transition.linkId`) gives `delivery_mismatch`.
  - Otherwise the checks and their order are unchanged: `fresh_mismatch` → `receipt_not_current` → `binding_moved` → `review_not_candidate` → `stale_content` → `uncertainty_overflow`.
- **Contract mapping** (034 §6, no new codes):
  - `strict_json` is the py-canon.v0 reader's refusal.
  - `delivery_mismatch` is the existing manifest/shape refusal ("a mandatory part that fails verification").
  - `fresh_mismatch` is "the revalidation proof is not about this exact candidate".

## 3. Tests

- **New `tests/test_consumer_hardening.py`** (30 cases):
  - depth 2000 and 100000, for arrays and objects;
  - a scan overflow after parsing;
  - valid moderate nesting still parses;
  - a depth-2000 receipt is refused (`strict_json`) before any effect;
  - each identity field missing, and malformed manifests;
  - a **fully re-signed** delivery that lacks only `attemptId` is refused at `verify_delivery`;
  - a `Fresh` missing each of six fields;
  - non-list `pending`/`queue`;
  - a `Verified` missing bound fields;
  - a well-formed proof still revalidates.
  **27 fail on the frozen `30ca084a`** (the 3 valid-input controls pass on both), and all pass on `aea15fa4`.
- **`tests/test_pilot_dryrun.py`:** `e1/consumer.py` moves from `FROZEN` to `REVISED_041` (`aea15fa4…`), with the pre-041 hash recorded as provenance. This follows the plan 038 `REVISED_038` precedent.
- The existing `tests/test_e2e_model.py` passes unchanged: 80 passed together with the new file.

## 4. Frozen bundles and replays

Both bundles are **byte-unchanged**: no `INDEX.sha256`, `producer.json` or file was touched.
- `fixtures/e2e-consumer-v0` (golden): **17/17 cases ok** with the revised consumer.
- `fixtures/e2e-byte-compat-v0` (supplement): **52/52 ok**.

Each replay pins the consumer hash from its frozen `producer.json` (`30ca084a…`), so each prints exactly one expected problem: `consumer drift … aea15fa4… != … 30ca084a…`. Root's decision on that line is recorded in §4a.

### 4a. Revised-consumer replay check (root msg 1748, option B)

- **The strict replays stay truthful.** Each bundle's own `replay.py` runs unchanged and still says **`REPLAY FAIL`** (exit 1). Its only problem is the declared source drift. Those logs are kept as the expected-drift evidence and are never relabelled.
- **A new out-of-bundle check, `e1/replay_revision.py`** (`python -m e1.replay_revision`), runs each strict replay and accepts its output **only** when all of these hold:
  1. the bundle's `producer.json` pin is exactly `REVISION_FROM` = `30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e`;
  2. the imported consumer is exactly `REVISION_TO` = `aea15fa421b5a857d9393dbbdff5073a977de5e28bf4a0854b35c0d1983475ed`;
  3. the strict problems are **exactly one** line, the declared drift in the bundle's own wording with both full hashes. Index, provenance, `handoff.py` and every case check therefore still run and must be clean;
  4. every case is `ok`, and the count equals the frozen count (golden 17, supplement 52), with no `BAD`;
  5. the strict verdict is its truthful `REPLAY FAIL`.
- **No allowances.** There is no generic ignore-drift switch and no broad `{old, new}` allowance. Any other hash, an extra problem, a missing or extra case, or a relabelled verdict rejects.
- **Report format.** For each bundle it reports the mode (`revised-consumer-replay`), the revision pair, the imported consumer and producer pin, the strict exit, verdict, problems and stdout sha256, the case counts, and `revisedResult`.
- **Result:** both bundles give **PASS** under the declared revision (golden 17/17, supplement 52/52). Each strict replay stays FAIL with only the declared drift.
- **Tests** (`tests/test_replay_revision.py`, 27 cases):
  - per bundle, the exact declared drift with every case ok is accepted;
  - rejected: the wrong imported consumer; the frozen consumer imported; a producer pin that is not the declared old one; an extra index problem; a `handoff` drift; a drift line naming another hash; no drift; a missing case; an extra case; a `BAD` case; a relabelled `REPLAY PASS`;
  - a live run of both bundles passes, and the bundle files are byte-identical before and after, with `git status` clean under `fixtures/`.
- **Adopting a future consumer revision** requires its own reviewed change to `REVISION_TO`. The bundles are never re-baselined; option C was rejected because it rewrites frozen provenance.

## 4b. Evidence logs (scratchpad, sha256 from tool output)

| Log | sha256 |
|---|---|
| strict golden replay (FAIL, declared drift only, 17/17 ok) | `5c055d462293379e270ffa33d1cbdc31be1366ae3e8b99a87afaed1dfe6d2f10` |
| strict supplement replay (FAIL, declared drift only, 52/52 ok) | `d73345690c43f4129f2c7e896a32f42a36c54d8bebb20f8d1ecbe37f7ef5e48b` |
| revised replay (`python -m e1.replay_revision`, PASS) | `f077642d38964e9d39ad87fd0fde38cc11979a082822ff492de708bd21f25531` |
| full default suite (991 passed, 1 skipped) | `8272d481d85046bb2143108f5c5657cf41fa9b3208bc65df0b62e3176a085810` |

## 4c. Parity with the ChatAgent TypeScript consumer (ChatAgent review msg 1752; a separate follow-up)

- **A missing identity field, with the identity object present.**
  - Python (this plan) refuses **at verification** with `delivery_mismatch`.
  - ChatAgent's TS `verifyStored` only checks that the identity object exists (`delivery.ts:451`). A missing field passes verification there and is refused **at revalidation** as `fresh_mismatch` (`sameReviewIdentity`, `delivery.ts:579–600`).
  - Both refuse the delivery, at a different stage and with a different code. Only a crafted, re-signed manifest can reach this case; the canonical producer never emits one.
  - **Follow-up, owned by ChatAgent:** CA-ISSUE-013 aligns TS verification (all identity fields required in `verifyStored`, giving `delivery_mismatch`). Root plans it as the next real Node-only supervised task (msg 1754). It is not part of this plan.
- **Deep nesting** (pre-existing; documented at `delivery.ts:59–62`):
  - TS refuses beyond 64 levels as `codec_unsupported` (DEPTH).
  - Python refuses only at its recursion limit, about 1000, as `strict_json`. Nesting from 65 to about 1000 levels is accepted by Python and refused by TS.
  - This plan does not change it, and no depth policy is adopted here.

## 5. Not changed or not claimed

- No other module changed: `handoff.py` is still `1e9036f6…`, matching the supplement's pinned value.
- No production state, no historical evidence and no frozen bundle bytes changed.
- No deeper redesign; no depth-limit policy.
