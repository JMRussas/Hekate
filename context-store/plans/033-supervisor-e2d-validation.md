# Plan 033 — E2d (selective-context handoff: package + review-lead rollover): validation evidence

**Status: accepted by codex-hekate (lead) for the bounded E2d fixture scope after independent review and validation of the frozen msg 1282 overlay.** Test-only, fixture-only, in a disposable database. Nothing is added to `PlanStoreSchema` or any production migration. There is no service, worker, provider, wake, send, bridge, ChatAgent or ChatRuntime change, and **no conversation is launched, woken or invoked**. Worker rollover (032 options R/S) and every production identity, auth and default choice are **deferred**.

**Date:** 2026-10-07
**Implementer:** claude-hekate. Execution model as reported by this session: **Claude Opus 5.5 (`claude-opus-5-5`)**.
**GO:** codex-hekate msg 1249 (E2d per the msg 1247 layout). In-flight reviews: msgs 1256, 1260, 1266, 1273 and the lead's four probes (`.review_tmp/e2d-lead-review-1272`, run unmodified from a temporary copy: all pass).
**Design:** [032](032-selective-context-conversation-handoff.md) revision 6, sha256 `60a3bdfa406f2ad13eaaf6300a05d63c04c2db9d58f008ca38e0c35df0983899` (frozen; unchanged by this work). Builds on the accepted E2c fixture ([031](031-supervisor-e2c-validation.md)), whose files are **unchanged**.

## Source basis and environment

| Item | Value |
|---|---|
| Hekate base | committed **`d0ed671`**. Clean-source gate: `git archive d0ed671` plus **only** the E2c + E2d overlay below (`…\scratchpad\hekate-d0ed671-e2d`, temporary); `diff -rq` against the workspace showed the overlay identical; the Api was built from the archive (it excludes the unowned uncommitted `Program.cs`/`CodeService.cs` edits) |
| Runtimes | uv 0.11.19; CPython 3.13.13; psycopg 3.3.6 (no dependency change); PostgreSQL in the owned `hekate-local` container (started in the E2c work, still running) |
| Limitations | the task part is **H1-shaped test data** (text, three instruction texts, `packageRef`); it is **not** rendered by ChatAgent's `buildPlanTaskContext`. Import authorization is an explicit **two-sided stub** (source read and destination use); real authorization belongs to the owning runtime (032 D10). The fixture clock is the adapter's `now` (as E2b-a/E2c) |

**Overlay (SHA-256).** E2d files are new. The E2c files are listed only because the gate needs them; their hashes equal the 031 acceptance.

| File | SHA-256 |
|---|---|
| `scripts/local/supervisor_e1/e1/handoff.py` (new) | `1e9036f6ed29983c1282dde0a3a256094984e2c6a9a3d8940a575beeea6faab2` |
| `scripts/local/supervisor_e1/e1/handoff_durable.py` (new) | `b6c1d9c3095c4bfba5bae865d5cf1dc4646d4c53e452ab3a95ab4c3ca5006670` |
| `scripts/local/supervisor_e1/e1/handoff_schema.sql` (new, fixture-only) | `affc532042b3399f24884358114b415847ab256f76d5eb183a05d2f14c223159` |
| `scripts/local/supervisor_e1/tests/test_e2d_model.py` (new) | `4186c77bd66dc013aac085098cc8fbd5ccb19a000d6c96c88a743047e9f748b2` |
| `scripts/local/supervisor_e1/tests/test_e2d_live.py` (new) | `11d9b46dde2744e5934c880a318452c17bc023c9e531a0deb6c2c416fc487d25` |
| `scripts/local/supervisor_e1/README.md` (edited: E2d section appended) | `015696a9647f36e4aab8d0ea307a60f2cae9c9ae38a8c7e0c340ba649d6567db` |
| E2c: `acts.py`, `acts_durable.py`, `acts_schema.sql`, `evidence.py`, `test_e2c_model.py`, `test_e2c_live.py` | unchanged (031) |

## Results

| Suite | Workspace | Clean-source gate (`d0ed671` + overlay, `uv run --locked`) |
|---|---|---|
| E2c baseline before E2d (031 acceptance) | **484 passed** | **484 passed** |
| `tests/test_e2d_model.py` (offline) | **29 passed** in 0.13s | included below |
| `tests/test_e2d_live.py` (live) | **21 passed** in 17.70s | included below |
| Lead probes (`test_e2d_lead_probes.py`, 4, temporary copy) + `test_e2d_live.py` | **25 passed** in 18.78s | — |
| Full default suite (incl. fault, E2a, E2b-a, E2c) | **534 passed** in 84.28s | **534 passed** in 83.44s |

534 = 484 (unchanged, all passing) + 29 model + 21 live.

### Independent lead validation

The lead independently extracted `git archive d0ed671` into `.review_tmp/e2d-final-review-1282`, copied only the exact E2c and E2d overlay listed above, and verified every overlay SHA-256 against msg 1282. The API built from that clean base excludes the unrelated workspace API edits. Using the existing fixture environment with `HEKATE_E1_CONTAINER_WORKSPACE=D:/Git/Hekate`, the full suite plus four independently authored probes completed with **538 passed in 85.06s**. The probes remain in the isolated review checkout as `scripts/local/supervisor_e1/tests/test_e2d_lead_probes.py`; they are not part of the source overlay.

The probes verify refusal of an unbound extra Task field, storage and exact redelivery of a valid 50,000-newline task despite JSON escaping, refusal when content was revised before prepare, and refusal to confirm a corrupted committed record on exact retry. Earlier independent pure probes exposed unbound envelope version/payload fields and an extra Task field; the accepted implementation closes those schemas. All four final probes pass. This acceptance covers the stated fixture behavior and limitations, not production activation, real authorization, or a real conversation handoff.

## The 032 §6 cases and where each is proved

| 032 §6 case | Model (`test_e2d_model.py`) | Live (`test_e2d_live.py`) |
|---|---|---|
| **1. Package and manifest**: every delivered byte bound (same basis, changed state / note / evidence / task text / instruction / pending / transition => new digest); determinism; candidate transition with no commit status; role policy (unproved mandatory refuses, unknown diagnostics carried, unlistable pending refuses); pending uncertainty always listed; required overflow refused; deterministic optional omission with the final partitions re-measured; note cap; accounting includes instruction texts | `test_identical_inputs…`, `test_import_text_or_provenance…`, `test_manifest_holds_a_candidate_transition…`, `test_unproved_mandatory_items_refuse`, `test_unknown_diagnostics_are_carried…`, `test_unconfirmed_ack_does_not_block_a_proven_candidate`, `test_open_intents_and_unconfirmed_appends…`, `test_unlistable_pending_effects_refuse`, `test_required_overflow_is_refused…`, `test_optional_items_are_omitted…`, `test_final_package_respects_both_partitions_at_the_boundary[4]`, `test_note_size_is_capped` | `test_prepare_lists_pending_effects_from_the_same_snapshot` |
| **2. Review-lead rollover, prepare / commit / activate**: prepare grants nothing; the immutable candidate; commit re-reads under the lock; a semantic change is `stale_candidate` but a diagnostic change is not; a real content change is stale; stale → re-prepare → commit; exact retry returns the committed record (retry check before the CAS); same `prepareId` + other content is a `conflict` and never overwrites; a bare repeated `review_rebind` is `stale_binding`; a lost reply is confirmed on the exact pre-assigned identity; the receipt verifies `current`, then `superseded`, and a forged one is `mismatch`; delivery failure → the same candidate with exact Task bytes; anchors unchanged and novelty continues; the release gate must be the old session; tampered caller objects and tampered stored candidates are refused | `test_freshness_compares_semantics_not_bases`, `test_decide_commit_order_retry_first_then_freshness_then_cas`, `test_handoff_ids_depend_on_who_and_the_prepare_operation…`, `test_semantic_set_reconstructs_from_the_stored_manifest`, `test_stored_candidate_verification_rejects_any_tampering` | `test_prepare_grants_no_authority…`, `test_commit_receipt_activation_anchors_and_novelty`, `test_semantic_change_between_prepare_and_commit…`, `test_real_content_change_between_prepare_and_commit_is_stale`, `test_stale_prepare_then_reprepare_then_commit`, `test_exact_retry_returns_the_committed_record…`, `test_same_prepare_id_with_other_content…`, `test_a_bare_repeated_review_rebind…`, `test_lost_commit_reply_is_confirmed_on_the_exact_identity`, `test_superseded_receipt_and_delivery_failure`, `test_release_gate_must_be_the_old_session`, `test_a_tampered_prepared_object_is_rejected`, `test_stored_candidate_tampering_is_detected` |
| **3. Snapshot discipline, envelope and imports**: mixed reads refused; versioned envelope beside the unchanged task part; imports keep the original ref + provenance + a destination mapping; hash mismatch, rewritten/repointed and ambiguous imports rejected; both authorization sides required; imports and notes are not acts | `test_a_package_mixing_reads_is_refused`, `test_bad_imports_are_rejected[4]`, `test_imports_need_both_sides_authorized[2]`, `test_imports_and_notes_are_not_acts` | `test_a_note_never_counts_as_progress` |
| **4. Crash and omitted proof (review role)**: an orphan candidate after a crash has no authority and can be committed by id from storage; an operator-gated transition without a quiescence proof; the old lead refused | — | `test_crash_after_prepare_leaves_an_orphan_without_authority`, `test_operator_gated_lead_transition_without_quiescence_proof` |
| **Documented, not hidden**: no activation fence | — | `test_post_commit_act_before_any_receipt_fetch_is_accepted_no_fence` |

## Contract choices

1. **Reuse without changing E2c.** Commit runs through the existing `ActsJournal._route_a` (project lock first, then global → writer → stream; one record per transaction; fault hooks; `CommitUnknown`). The decision reads the candidate row, the record under the pre-assigned id, `outstanding`, counters and queue on the **same session and transaction** (a second cursor on the route A connection). No E2c or E2b-a module was edited.
2. **Prepare is one snapshot.** C-B, the validated stream, its `outstanding` reservations, counters and queue come from one REPEATABLE READ READ ONLY transaction. Pending effects are open intents (cross-checked against `outstanding`) plus caller-held unconfirmed appends with their `confirm()` status. A corrupt stream makes them unlistable, so the package is refused.
3. **Liveness from PlanStore (msg 1256).** The mandatory check uses `A.review_state` from the same snapshot. The C-B review/ACK/progress values are diagnostics, so an unconfirmed ACK does not block a proven candidate.
4. **Identity (msg 1256).** `handoffId = uuid5(root, claimKey, reviewId, predecessor, target, prepareId)`. The record id and link id derive from it. A caller-supplied `prepareId` separates exact retries from genuinely new prepares.
5. **The stored candidate is the authority for commit (msgs 1260, 1266).** The immutable row holds the digest, the manifest, the exact canonical envelope and the exact Task bytes. Commit and redelivery verify: the digest equals `sha(JCS(manifest))`; the envelope equals, byte for byte, the canonical envelope of that manifest and its bound payload (no other version, keys or bytes); the task text, each instruction text, each import and the note hash to the manifest. The transition and the prepare-side semantic set are reconstructed from the stored manifest only, and a caller `Prepared` that differs is `tampered_candidate`.
6. **Freshness (032 §3a; msg 1260).** The semantic set holds the exact key, the PlanStore class, the `packageRef`, the **current** PlanStore pins read under the lock (content revision, attempt pins, artifact, dependency-edge digest), the predecessor, and the pending-effect ids and statuses and queue reasons. Bases and transaction ids are excluded; diagnostics do not invalidate.
7. **Accounting.** bytes = task text + the three instruction texts + `JCS(envelope)` (manifest and `selection` included). The final package is re-measured with its final `selection`, and trailing optional items are dropped until required ≤ 64 KiB / 256 references, optional ≤ 32 KiB / 64 references and total ≤ 96 KiB.
8. **No activation fence.** As 032 §3a states, binding validation accepts the successor once the commit is durable; this is tested and documented, not hidden.

9. **Closed stored forms (msgs 1266, 1273).** The stored envelope must be byte-for-byte the canonical envelope of its manifest and bound payload (version `handoff-envelope.v0`, keys exactly `{version, manifest, payload}`, payload keys exactly `{imports, note}`). The stored task must be exactly `{text, instructions{system, fast, deep}, packageRef}`, all strings, stored as its canonical bytes. Any unbound field or byte is `tampered_candidate`.
10. **Prepare refuses stale content (lead probe).** Prepare applies `PlanRules.PinFailure` as far as one node row can: an unpinned attempt, content revised since the pin, or a supplied package whose pins are not the attempt's gives `stale_content` (live: content revised before prepare).
11. **A corrupt stream never confirms (lead probe).** If the stream does not validate (e.g. a record hash was rewritten), commit returns `corrupt`, even for an exact retry, and issues no receipt.
12. **Storage bounds allow JSON escaping (lead probe).** Delivered-byte caps are enforced before storage; the stored `manifest`/`envelope`/`task` columns allow up to 1 MiB of escaped JSON (live: a 50 KB task of newlines is stored and redelivered exactly).

## Findings for the lead

- **Activation fence:** none exists (by design in 032); a successor that skips the receipt check is not stopped by the binding layer.
- **Task part:** H1-shaped test data only. Using the real H1 renderer for the Task part needs the opt-in pinned interop suite and is not claimed here.
- **Tamper detection** relies on the immutable row plus hashes; a credentialed user who bypasses triggers and rewrites the manifest, envelope and digest consistently is not detected (integrity, not authorization; as E2b-a).
- **Queue side effects:** a refused probe act from the target adds a `foreign_session` queue reason, which is a semantic change and correctly stales an outstanding candidate.

## Not claimed

- Worker rollover (R or S), the D1/D2 identity choice, objective-level time (D4), or any production identity, auth or default.
- Any conversation launch, invocation, wake, ACK or delivery mechanism; ChatAgent or ChatRuntime rendering; real authorization for imports.
- An activation fence; lifetime uniqueness of handoff ids beyond retained rows (candidates leave with their stream on compaction).
- No commits were made.
