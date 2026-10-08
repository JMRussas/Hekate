# Plan 042 — plan-run v0: drive dependent task nodes of an imported plan (P1, offline)

**Status: implemented, pending independent review (2026-10-08).**
- **Branch:** `feat/plan-run-v0` from `d6642f0`. GO: root msg 1793, P1 offline only; scope note msg 1806.
- **Design:** ChatAgent msg 1786 and Hekate msg 1789.
- **Test scope:** disposable harness database, fake CLI, fake node tools, temporary repos. No model, no live database.
- **P2** (a persistent store, HK-ISSUE-009/010) is not authorized. Its design is a separate, short proposal.

## 1. What it does (the minimal complete slice)

```
plan-import.v0 JSON ──► PlanStore plan (root + task nodes + Accepted-gate edges)        e1/plan_import.py
run_plan(plan):                                                                          e1/plan_run.py
  one authoritative GET /plans/{root}
    ├─ in flight / rejected / cancelled / plan drift ──► STOP needs_operator (nothing claimed)
    ├─ every imported node accepted ─────────────────► all_done   (the ONLY success)
    ├─ nothing ready ────────────────────────────────► STOP needs_operator, blockers named per node
    └─ first ready node (PlanStore's claim order):
         spec pinned in the node value? (pending ──► STOP spec_pending)
         spec file still hashes to the pin? (else STOP spec_mismatch)
         every accepted predecessor artifact an ANCESTOR of the spec base? (else STOP base_not_chained)
         a NEW per-node run root (existing ──► STOP: an earlier attempt)
         existing preflight ──► existing supervised pilot ATTACHED to that node
            claim must return exactly that node (else claim_mismatch: nothing dispatched)
            receipt must pin exactly the checked content and predecessor artifacts (claim_check)
            existing worker + independent spec verifier (all steps) + PlanStore decision
         accepted? next iteration : STOP node_not_accepted (successors stay blocked by PlanStore's rule)
```

**Artifact forwarding is an operator step (D3 v0, root msg 1793: no automatic integration).**
- A successor's base must contain its predecessor's accepted artifact, so its spec can only be frozen after that predecessor is accepted.
- The import therefore allows `spec: null` (pending).
- The operator then:
  1. fetches the accepted artifact into the source repo;
  2. commits the successor's oracle on top, on a branch;
  3. freezes the spec;
  4. pins it with `plan_import.pin_spec`, which revises the node's content value through the existing content route.
- A re-run continues. The driver *verifies* the forwarding (ancestry, then the claim receipt's predecessor pins); it never performs it.

## 2. Authority and identities (no second ledger)

- **PlanStore is the only state.** The driver keeps no status file; `plan-run-<n>.json` logs are evidence only. Each iteration re-derives everything from one plan view, which is a single coherent read.
- **Spec reference (D2):** the node content value is `task-spec.v0 sha256=<hex> path=<abs path>`, or `task-spec.v0 pending`. There is no new attribute key and no PlanStore change.
- **Deterministic identities:** `root = uuid5(NS, plan-import.v0|project|sha256(import bytes))` and `node = uuid5(root, key)`.
- **Why reconciliation is needed.** A probe showed that PlanStore does **not** replay an operation key once the revision has moved on: it returns `plan_exists` or `stale_revision`. So the import reconciles against the plan view:
  - an existing step equal to the document is skipped;
  - a missing step is applied at the current revision;
  - other content, an extra node or edge, or another project is a typed `import_conflict`, and nothing is written.
- **Result:** a re-import is a no-op, and a resume after a crash mid-import completes without duplicates.
- **Validation before any write:** the closed shape, keys, graph (known keys, no self edge, acyclic, 1..8 nodes) and every given spec (loads, validates, matches its sha256).
- **No re-claiming:**
  - a node that is `in_progress`, or `done` without a current acceptance, is **in flight**: a re-run stops and classifies it instead of claiming it;
  - an existing per-node run root is an earlier attempt, so it stops too.

## 3. Changes to existing code (the default behaviour is unchanged)

- `e1/pilot.py`: optional `attach=(root, leaf)`, which skips `create_plan`, and optional `claim_check(receipt)`, which stops before dispatch as `claim_check_failed`. In attach mode the `claim_mismatch` detail names the claimed node; the non-attach detail is unchanged.
- `e1/task_runner.py`: `run(..., attach=, claim_check=)` is passed through to the pilot.
- Test fakes:
  - the fake vitest honours test-file operands and an optional per-case `source` file;
  - the fake CLI gains `other_ok` / `other_bad` (the successor task).

## 4. Tests (`tests/test_plan_run.py`, 26) and the demonstration

**Import**
- 13 invalid documents plus an invalid spec file are each refused with a typed code, **with zero writes** (a recording stub).
- A 3-node chain is created with deterministic ids; only `a` is ready; a re-import applies nothing and leaves the view byte-equal.
- A crash on the second child, followed by a re-import, completes exactly the missing steps with no duplicates.
- A conflicting existing node gives `import_conflict` and the view is unchanged.

**Driver**
- **End to end:**
  1. run 1: `a` is accepted, then `spec_pending(b)`;
  2. the operator forwards `a`'s artifact and pins `b`'s spec;
  3. run 2: `b` is accepted, then `all_done`;
  4. run 3: `all_done` with nothing claimed.
- `base_not_chained`: the successor base is not on `a`'s artifact. The run stops with no clone and `b` stays Todo.
- `spec_mismatch`: the pinned file changed. The run stops before preflight.
- **In flight:** a node claimed by someone else makes the run stop with no new claim.
- **Rejected:** `a` is rejected at max rounds, so `node_not_accepted`; a re-run gives `node_rejected`, and `b` stays blocked.
- An existing node run root stops the run.
- `no_ready_work` is **never** done, and the blockers are named; done, awaiting-review, stale and cancelled states are classified.
- The claim check catches a wrong node, content pin or predecessor artifact or acceptance.
- An attached pilot whose claim returns another node gets `claim_mismatch` and **the worker is never called**.

**Demonstration:** `uv run python tests/demo_plan_run.py` walks the whole slice and prints each step; it ends with RESULT PASS.

## 5. Not in v0 (explicit)

- automatic integration (forwarding is an operator step);
- resume of an in-flight node (it stops);
- a persistent store or a real clock (P2: HK-ISSUE-009/010);
- parallel nodes;
- spec templating;
- a real fix round across nodes.

The HK-ISSUE-005 fix-round workaround is inherited from the pilot, unchanged.
