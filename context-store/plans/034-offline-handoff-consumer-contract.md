# Plan 034 — Offline handoff consumer contract (design proposal)

**Status: design-only proposal, revision 3 (folds in source follow-up msg 1294 and lead reviews msgs 1297, 1299 and 1301; changes in §10–§11), for review by codex-hekate (lead). No implementation GO.** It closes [032](032-selective-context-conversation-handoff.md) D10 (import mechanism and authorizer) and D11 (placement of the handoff block), and this document's own C5/C6, **for an offline fixture/adapter contract only**. Nothing here changes ChatAgent, ChatRuntime, the bridge, PlanStore, a production source store, auth, identity or defaults. Nothing launches, wakes or invokes a conversation or a worker. The accepted 030–033, E2c and E2d sources are **unchanged**.

**Task:** codex-hekate msg 1290.
**Base:** Hekate `d0ed671` + the accepted E2c/E2d overlay (031, 033).
**Pinned contract vs current HEAD:** the H1 contract this design and its tests rely on is the **pinned** commit `5255daacfc670a4919f61439eb12adcb6a401920` with Node `24.21.0`, as `e1/h1_bridge.py` enforces. The active ChatAgent checkout is at `dc7d41241ce318bae689305c6e5a5b93612e23f0`; `planTask.ts`, `contextBuilder.ts`, `sourceStore.ts` and `domain/context.ts` are byte-identical between the two commits (`git diff 5255daa dc7d412` is empty for them) and clean in the working tree, so the facts below hold for both, but tests pin `5255daa`.

**Labels:** **V** = verified in source or by a probe run in this session; **I** = inferred; **P** = proposed here.

## 0. Problem and scope

E2d (033) produces an accepted, immutable candidate (an E2d `handoff-envelope.v0` with a Python-canonical manifest, `py-canon.v0` here) and a commit receipt. Nothing defines how a **consumer** receives it, checks it, bounds it and composes it with the real, unchanged H1 package. This document defines that consumer offline, for the **exact accepted v0 bytes**.

**In scope:** a new delivery wrapper; verifying exact bytes; a separately identified **consumer view** (authorization and budget projection) that never alters the committed candidate; composing the view with the real H1 message; the import/retrieval policy stub; bounded retrieval; the pinned estimator; fresh revalidation; refusal vs unavailability; identity; no ownership effects.
**Not in scope:** a real conversation or worker launch, any model call, an invocation fence, a production source store, auth, identity, defaults, bridge changes, any ChatAgent edit, and **any new codec, manifest version or producer** (see §8).

## 1. Source facts

| Fact | Source |
|---|---|
| **V:** H1 `buildPlanTaskContext` calls `buildContext` with `events: []`, `currentMessageId: claimKey`, `currentUserText: input.text`, the caller's `systemInstruction`, `roleInstructions {fast, deep}` and `budget`, then **asserts** exactly one `user` message equal to the package text, `memory === null`, no resolved/unavailable sources, no included/omitted turns, no active tasks and `budgetUsage.references === 0`; otherwise it throws | ChatAgent `src/integrations/hekate/planTask.ts` 452–497 |
| **V:** H1 refuses with stable codes that never echo content: `INPUT_TOO_LARGE`, `INVALID_NUMBER`, `INVALID_RESPONSE`, `UNSUPPORTED_CONTRACT`, `NO_WORK`, `INVALID_RULES`, `PACKAGE_TOO_LARGE`, `INVALID_OPTIONS`; a context that does not fit returns `ContextBudgetError` (`CONTEXT_TOO_LARGE`), nothing truncated. Its options schema rejects NaN, infinite or negative reserves (`INVALID_OPTIONS`). Default limits: response 1 MiB, prerequisites 256 KiB, 32 rules / 256 KiB, package 2 MiB | `planTask.ts` 22–62, 374, 433–450 |
| **V:** the H1 result carries `text`, `suppliedSha256 = sha256(text)` and a `source` block of hashes; its context `snapshotId` is a fresh random UUID | `planTask.ts` 359–410, 427–431 |
| **V: the pinned estimator.** `utf8ConservativeTokenCounter.estimateBytes(text) = Buffer.byteLength(text, "utf8")`; `REQUEST_OVERHEAD_TOKENS = 32`, `MESSAGE_OVERHEAD_TOKENS = 16`; `availableInputTokens = windowTokens − max(fastOutputTokens, deepOutputTokens) − safetyTokens`; fixed cost = 32 + bytes(rendered system with the larger role instruction) + 16 + bytes(current user text) + 16; `fixedCost > availableInputTokens` ⇒ `CONTEXT_TOO_LARGE` | `src/app/contextBuilder.ts` 24–41, 186–216 |
| **V:** `buildContext` **can** carry `memory`, `resolvedSources` and `unavailableSources` (which a host may drop under budget), but H1 forbids all of them | `contextBuilder.ts` 64–82; `context.ts` 49–66 |
| **V:** the source store is conversation-scoped: `resolve` refuses another `conversationId` and checks `contentHash` = SHA-256 hex of the UTF-8 text; records carry `provenance: user-stated \\| assistant-claimed` | `src/app/sourceStore.ts` 1–12, 54–71 |
| **V:** Hekate pins H1 at `5255daa` with Node `24.21.0` and runs it only through the opt-in interop suite (`e1/h1_bridge.py`: pinned commit, clean tracked sources, preinstalled `tsx`, the exact Node version, never PATH). A missing prerequisite makes the suite **error, never skip**. The README permits a detached pinned clone whose `node_modules` is a shared junction and forbids resetting the active ChatAgent checkout. No install is needed when the pinned runtime is present | `e1/h1_bridge.py` 1–95; supervisor_e1 README (E1b, line 85) |
| **V:** the E2d envelope has exactly `{version, manifest, payload}` (payload exactly `{imports, note}`), **no codec field**, and extra keys are refused; the manifest's `selection` is fixed at prepare; the digest and the receipt bind those exact bytes | `e1/handoff.py` `build`, `verify_stored`; 033 choice 9 |
| **V:** the E2d Task part in existing candidates is H1-**shaped** test data, not an H1 rendering | 033 "Limitations" |
| **V (probe, this session):** `py-canon.v0` (Python `A.canonical`) is not RFC 8785 and differs from JavaScript: `1000.0`/`-0.0` vs `1000`/`0`; code-point key order (U+FB01 before U+1F600) vs JS UTF-16 order (reversed); a lone surrogate cannot be encoded vs JS escapes it; `2^60` exact vs JS `1152921504606847000`. The E2d manifest **contains floats** (fake-clock `at`, `anchorAt`, `deadline`) | `e1/acts.py` `canonical`; CPython 3.13.13 and Node probes |

## 2. The delivery wrapper (P; new, the E2d envelope is untouched)

The consumer receives one **delivery**: a new wrapper object around the unchanged accepted bytes.

| Field | Content |
|---|---|
| `wrapper` | `"handoff-delivery.v0"` |
| `codec` | `"py-canon.v0"`, **fixed**: it declares how the enclosed bytes were produced; it is not inside, and does not change, the E2d envelope |
| `candidateDigest` | from the receipt |
| `manifestBytes` | the **exact** stored manifest bytes (UTF-8) |
| `envelopeBytes` | the **exact** stored envelope bytes |
| `taskBytes` | the exact stored task JSON (text + system/fast/deep + `packageRef`) |
| `receipt` | the E2d commit receipt |
| `h1Input` | the inputs the pinned H1 takes, unchanged: raw claim response bytes, rules, limits, `systemInstruction`, `roleInstructions`, `budget`, `capturedAtIso` |

**Ingress caps, checked on raw bytes BEFORE any parsing** (msg 1301; fixture constants). These bound the **transport**, which carries the manifest twice (once as `manifestBytes`, once inside `envelopeBytes`) and stores JSON-escaped text, so they are separate from, and larger than, 032's delivered-content accounting:

| Bytes | Cap |
|---|---|
| `manifestBytes` | ≤ 1 MiB (the E2d storage bound) |
| `envelopeBytes` | ≤ 1 MiB |
| `taskBytes` | ≤ 1 MiB |
| `receipt` | ≤ 4 KiB |
| `h1Input` | the pinned H1 limits: claim response ≤ 1 MiB, rules ≤ 32 / 256 KiB, each instruction text ≤ 64 KiB |
| **whole wrapper** | ≤ **4.5 MiB**, checked first; anything larger is refused unread (`ingress_too_large`) |

After parsing, 032's **delivered-content** caps are re-checked on the decoded content (required ≤ 64 KiB / 256 references, optional ≤ 32 KiB / 64 references, total ≤ 96 KiB, counting the task text and the three instruction texts); a violation is `delivery_overflow`.

**Exact-byte verification (all consumers):** `sha256(manifestBytes) == candidateDigest == receipt.candidateDigest`. The envelope, task and payload bytes are checked against the manifest as E2d `verify_stored` does. **Any digest, envelope, task or payload mismatch refuses the whole delivery.** "No re-encoding" applies to **digest verification**: a digest is only ever computed over the received bytes. The Python consumer's canonical-**shape** comparison (re-encoding the parsed envelope and task to confirm they are exactly the canonical form of what the manifest binds, as `verify_stored` does) is allowed and is part of verification.
**Cross-language:** the Python consumer parses `py-canon.v0`. A Node check (pinned runtime) recomputes `sha256` over the **same bytes** and compares; it never re-encodes. That is sufficient for this scope (msg 1297).

## 3. The consumer view (P; the committed candidate is never altered)

The consumer never drops items from, or updates `selection` in, the committed manifest. Everything it decides goes into a separate **consumer view**:

- **Content:** the mandatory envelope content, verbatim from the candidate; the optional items the candidate included, each marked `included`, `denied{rule}` (policy), `unavailable{reason}` (retrieval or verification failure), or `omitted{budget}` (the consumer's own budget); retrieval results (§5); the fresh revalidation facts (§6); and the H1 binding (§4).
- **Identity:** `viewDigest = sha256(py-canon.v0(view))`, where `view` is the preimage and **does not contain `viewDigest`** (msg 1301). It is a **new** identity that binds (msg 1297):
  - `candidateDigest` and `receipt.recordId`;
  - `principal` and the destination `conversationId`;
  - the **actual** policy decisions (per item: the rule matched, allow/deny, the token) and a digest of the full rule set, not just its version;
  - the projected content (the digest of every included item's bytes) and every retrieval result or `unavailable` wrapper;
  - the H1 `suppliedSha256` and instruction digests;
  - the `budget`, the estimator id (`chatagent-utf8-conservative-v1@5255daa`) and the consumer's reservation, carried as a **fixed-width field** (below).
- `candidateDigest` is **never** reused as the digest of projected content.
- **No cache:** each composition re-evaluates the policy, re-runs retrieval and re-reads the revalidation facts. Nothing from a previous composition is reused.
- Legitimately denied or unavailable optional items are represented in the view; they never refuse the delivery.

## 4. Composition with the real H1 (P; closes D11 offline)

- **The real H1 is the Task at prepare.** The next increment runs the pinned H1 on a real claim **first** and passes its exact `text` and `systemInstruction`/`roleInstructions` as the E2d prepare's task. A consumer never substitutes H1 output into an existing H1-shaped candidate: candidates prepared from H1-shaped data stay consumable only by the Python fixture and are refused by an H1-bound consumer (`task_mismatch`).
- **Binding:** at the consumer, the pinned H1 is re-run with the delivery's `h1Input`. Its `text` must equal the delivered task text, its `suppliedSha256` the manifest's, and the SHA-256 of its system/fast/deep instructions the manifest's instruction digests. Otherwise `task_mismatch`.
- **H1 stays exactly one message;** the consumer view is rendered as a **separate mandatory part** (not an H1 message, not `memory`, not `resolvedSources`), with its own reserved budget. If it does not fit beside H1, the composition is refused; only optional view items may be `omitted{budget}`, in a fixed order (retrieval results, then imports, then the note), recorded in the view.
- **Attributed data only:** notes, imports and retrieved items are rendered **inside the view part as attributed data** (with their provenance labels); they are never placed in, or appended to, `systemInstruction` or `roleInstructions`.
- **Budget with the pinned estimator** (`estimateBytes` = UTF-8 byte length, not bytes ÷ 4): the view part costs `estimateBytes(renderedViewPart) + MESSAGE_OVERHEAD_TOKENS (16)` tokens, where `renderedViewPart` is the **complete emitted part**: its framing (part header, item labels, separators), the `py-canon.v0(view)` bytes **and** the `viewDigest` line. Every emitted byte is charged.
- **No self-reference in the reservation** (msg 1301): the view carries `reservationTokens` as a **fixed-width, zero-padded 10-digit decimal string** (e.g. `"0000004213"`). The consumer renders the part with `"0000000000"`, measures it, then writes the real value; the byte length is unchanged, so the measured cost is exactly the final cost (a fixed point by construction). `viewDigest` is computed over the view **after** the value is written and is not part of its own preimage. A cost that would need more than 10 digits is `CONTEXT_TOO_LARGE`. The consumer calls H1 with `windowTokens' = windowTokens − viewCost`. If `windowTokens'` would leave `availableInputTokens < 0` (or H1's schema would reject it), the consumer returns the **typed** `CONTEXT_TOO_LARGE` itself, before calling H1, never `INVALID_OPTIONS`. If H1 returns `CONTEXT_TOO_LARGE`, the composition is refused.

## 5. Imports and bounded retrieval (P; closes D10, C5 and C6 offline)

- **Policy stub, default deny:** `{version: "import-policy-stub.v0", principal, rules}`. Per item it answers exactly two questions, may `principal` read the source, and may it use it in this destination, and returns a token for each yes. Anything not matched is no. A fixture stub, never production auth.
- **Imports** keep their original `{conversationId, eventId, messageId, contentHash}` and provenance plus `{destination conversationId, importId}`, are re-hashed against both `textSha256` and `contentHash`, are rendered `imported:<provenance>`, and are never repointed or fed to the destination's source store.
- **Retrieval (C5 closed):** only for pointers the manifest's evidence index selected; nothing else is fetchable. Fixed caps per composition (fixture constants): at most **16 calls**, **64 returned items**, **32 KiB** of returned bytes. Each result keeps its read basis and is labelled `as-of`; it is never current and never ownership proof. Source-read **and** destination-use policy is re-checked for every retrieved item. Every returned item **and** every `unavailable` wrapper is counted in the final budget.
- **Estimator (C6 closed):** the exact pinned H1 estimator and framing (§1) is used for every part the consumer adds; no other tokenizer.
- **Optional vs mandatory:** imports, the note and retrieval results are optional (deny or unavailable is shown in the view). The envelope's mandatory content and the H1 binding are mandatory: any failure refuses.

## 6. Revalidation, refusal, no effects (P)

- **Fresh required facts** (msgs 1297, 1299), read together in **one combined snapshot** (030 currentness rule) immediately before the composition is returned. The receipt's status is evaluated **inside that same snapshot** with the E2d `receipt_status` logic over the snapshot's validated records; the consumer does **not** call `verify_receipt` (its own snapshot) and then read the other facts separately, which could race:
  1. the receipt's record is present, chain-valid and `current` in this snapshot;
  2. the current binding is exactly `receipt.bindingLinkId` (the successor);
  3. the review is still a PlanStore `candidate` for the exact ReviewKey (`review_state`);
  4. the task pins are current (`pins_problem` is none);
  5. the pending-uncertainty set (open intents + unconfirmed appends) and queue reasons.

  Items 1–4 failing refuses (`receipt_not_current`, `binding_moved`, `review_not_candidate`, `stale_content`). Item 5 is **re-listed in the view's mandatory part as of revalidation**, beside the manifest's as-of-prepare list, so new uncertainty is always visible.
- **Caps and budget AFTER the final re-read** (msg 1301): the composition order is revalidate → build the view → measure → call H1 with the reduced window → final check. After the re-read, the current uncertainty and queue list must fit **≤ 256 references** in the mandatory part (it can never be omitted; over the cap is `uncertainty_overflow`); retrieval must have stayed within its caps (§5); and the final emitted view part plus the H1 context must fit the `budget` **as recomputed from the fresh facts**. Any failure refuses.
- **No invocation fence and no invocation:** the final coherent read is **as-of** that snapshot only. It narrows, never closes, the window before a (future) invocation; there is no atomic fence and E2e invokes nothing. A crash after revalidation means the next attempt revalidates again.
- **Refusals (no view returned):** `digest_mismatch`, `envelope_mismatch`, `task_mismatch`, `codec_unsupported`, the four revalidation failures, any H1 refusal, `CONTEXT_TOO_LARGE`, and a mandatory part that fails verification.
- **No ownership effects:** verification, policy, retrieval, view building and composition **write nothing** (no record, counter, queue entry, binding or PlanStore change), which is testable as an unchanged journal-table digest.

## 7. Smallest next increment: offline E2e (proposal; no GO requested)

New files under `scripts/local/supervisor_e1` only; no ChatAgent edits, no installs; accepted files unchanged.

| File | Role |
|---|---|
| `e1/consumer.py` (pure) | delivery-wrapper verification over exact bytes; the consumer view and `viewDigest`; the policy stub; retrieval caps; the pinned-estimator port (bytes + 16/32 framing, `availableInputTokens`); the H1 binding check; refusal vs view-item rules |
| `e1/consumer_durable.py` | the one-snapshot revalidation read (receipt, binding, review class, pins, pending/queue) and the bounded retrieval adapter (C-B pages for selected pointers only) |
| `e1/sha_check.mjs` | a few lines: sha256 over a bytes file, run under the pinned Node; no re-encoding. Used **only** by the opt-in interop suite |
| `tests/test_e2e_model.py` | tamper of manifest/envelope/task/payload refuses the whole delivery; denied and unavailable optional items in the view; the committed manifest is never altered; `viewDigest` changes with principal, destination, rule set (same version), policy decision, retrieved content and budget; no cache across compositions; retrieval caps and counting of `unavailable` wrappers; the estimator port and typed `CONTEXT_TOO_LARGE` on underflow; py-canon.v0 corpus (Unicode, astral, controls, floats, duplicate keys rejected by the strict reader) |
| `tests/test_e2e_live.py` | **default suite, Python/Postgres only:** E2d prepare (with H1-shaped data) → commit → delivery → view; ingress caps refuse unread; revalidation refusals (receipt superseded, binding moved, review decided, content revised); new pending uncertainty re-listed; the fixed-width reservation fixed point; the final budget re-check after the fresh read; journal-table digest unchanged |
| `interop/test_e2e_node_sha.py` (opt-in) | the pinned Node `sha_check.mjs` over the exact delivery bytes and a py-canon.v0 corpus (Unicode, astral, controls, floats, duplicate keys) equals Python's SHA-256; **errors** (never skips) when the pinned runtime is missing, and only when this suite is selected |
| `interop_live/test_e2e_h1.py` (opt-in) | the pinned real H1 on a real claim → its output as the E2d task **at prepare** → commit → consumer re-runs H1 and binds it; exactly one H1 message; `windowTokens` reduced by the view cost; `CONTEXT_TOO_LARGE` at the boundary; errors (never skips) without the pinned prerequisites, and only when this suite is selected |

The **default** `uv run pytest` stays Python + Postgres only; missing Node or H1 prerequisites can fail only the opt-in suite that was selected, never the default suite.

## 8. Future, optional, not a prerequisite

A cross-language **canonical codec** (for example a restricted `hk-canon.v1` with no floats, safe integers and ASCII keys, matching RFC 8785 on that subset) and a v1 producer are **separate future work**. They cannot apply to accepted candidates: changing clock or manifest bytes changes the digest, which would no longer match the committed receipt.

## 9. Unresolved decisions

| # | Decision |
|---|---|
| C1 | Whether a cross-language canonical codec is ever wanted (§8) |
| C3 | Where the consumer view would go in a real runtime (ChatAgent has no slot; the offline contract stops at `{h1Context, viewPart}`) |
| C4 | The policy stub becoming a real authorization interface (owner: the runtime that owns the source store) |
| C7 | The fixture retrieval caps (16 calls / 64 items / 32 KiB) as production defaults (not proposed) |

C5 (retrieval) and C6 (estimator) are closed in §5. C2 (clock representation) is moot without a new codec.

## 10. Revision 2 changes (msg 1297)

1. **Exact v0 bytes only (§0, §2, §8):** a new `handoff-delivery.v0` wrapper carries a fixed `codec: py-canon.v0` beside the unchanged E2d envelope; no v1 producer or migration; Python consumer + Node exact-byte SHA are the interop scope; a canonical codec is optional future work.
2. **Consumer view (§3):** the committed manifest and `selection` are never altered; authorization and budget projection form a separately identified view (`viewDigest`), with denied, unavailable and omitted items listed; tampering refuses the whole delivery.
3. **View identity (§3):** binds principal, destination, the actual policy decisions and the full rule-set digest, projected content, retrieval results, H1 digests, budget, estimator and reservation; no cached authorization or currentness.
4. **C5/C6 closed (§5):** retrieval only for manifest-selected pointers, capped (16 calls / 64 items / 32 KiB), every result and `unavailable` wrapper counted, policy re-checked per item; the exact pinned estimator and framing; underflow gives the typed `CONTEXT_TOO_LARGE`.
5. **Revalidation (§6; msg 1299):** one combined snapshot evaluates the receipt status (with the E2d `receipt_status` logic, not a separate `verify_receipt` snapshot), the current binding, the review class, the task pins and the pending uncertainty/queue; the result is as-of only; explicit no invocation fence and no invocation.
7. **Estimator and framing (§4; msg 1299):** UTF-8 byte length, 32/16 overheads, and every framing byte of the view part charged; notes, imports and retrievals are attributed data, never system or role instructions.
6. **Real H1 at prepare (§4):** the pinned H1 output is the E2d task at prepare; it is never substituted into an existing H1-shaped candidate.

## 11. Revision 3 changes (msg 1301)

1. **Ingress vs delivered-content caps (§2, §6):** raw-byte caps before parsing (manifest, envelope, task each ≤ 1 MiB; receipt ≤ 4 KiB; H1 inputs at the pinned H1 limits; whole wrapper ≤ 4.5 MiB, refused unread), distinct from 032's decoded delivered-content caps; and caps re-checked after the final re-read (uncertainty ≤ 256 references, retrieval caps, final budget from the fresh facts).
2. **No self-reference (§3, §4):** `viewDigest` is outside its preimage; the reservation is a fixed-width 10-digit field whose rendered length does not change when filled in, so the measured cost equals the final emitted cost (framing and digest line included); the final budget is re-checked after the fresh facts.
3. **Default suite unchanged in kind (§7):** Python/Postgres only; the pinned Node SHA corpus is in opt-in `interop/`, the real H1 in opt-in `interop_live/`; a missing pinned prerequisite fails only the selected opt-in suite.
4. **"No re-encoding" is about digests (§2):** the Python canonical-shape comparison (`verify_stored`) is explicitly allowed.
