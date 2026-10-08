# Plan 044 — plan-run D3 v1: a successor's base derived from the accepted predecessor (offline)

**Status: implemented, pending independent review (2026-10-07).**
- **Branch:** `feat/plan-run-d3v1` from `d4b20ca` (P1 accepted).
- **GO:** root msg 1831, with corrections (i) and (ii); proposal msg 1825.
- **Test scope:** disposable harness, fake CLI, temporary repos. No model, no live DB, no push, no edits to the primary repo.

## 1. What changes for the user

P1 (plan 042) needed a manual operator handoff between dependent tasks. **D3 v1 removes it for a linear predecessor → successor pair.** One `run_plan` invocation runs A, then derives B's base from A's accepted artifact, then runs B, and ends `all_done`.

## 2. successor-recipe.v0

The recipe is a closed file, pinned at import (`"spec": {"recipe": {path, sha256}}`), so the node value is `task-recipe.v0 sha256=… path=…`. It is validated **before any write**:

```
{"version": "successor-recipe.v0",
 "template": <supervised-task-spec.v0 whose source is exactly {repo}: NO anchorCommit / taskBaseCommit>,
 "oracle": [{"path", "sha256", "from": <absolute file with the exact bytes>, "replaces": null | <expected old blob sha256>}]}
```

- **Template:** it is a valid v0 spec once given an anchor and a base.
- **Oracle list:** `oracle` covers **exactly** the template's `oracle.files`, with the same paths and sha256.
- **Oracle entries (correction ii, no unchecked overwrite):**
  - `replaces: null` means a **new** file, which must be absent at the predecessor's artifact.
  - Otherwise the file must exist there with **exactly** the pinned old blob before it is replaced.
- **Linear only:** a recipe node must have exactly one predecessor; anything else is refused at import.

## 3. Materialization (when the recipe node is next; every failure is a typed stop BEFORE any preflight clone or claim)

1. **Re-hash** the recipe and every `from` file (`recipe_tamper`).
2. **Bind the predecessor to its own run (correction i):**
   - its PlanStore `artifactRef` X equals the single `ownedRefs` entry in its evidence, and that ref resolves to X in its **owned** clone (`artifact_unavailable`);
   - the evidence `specSha256` is the spec it actually ran with (its pinned spec, or its own resolved spec, whose provenance names the same recipe pin);
   - X's parent is that spec's base (`predecessor_base_mismatch`).
3. **Owned integration repo** `<run_root>/<key>.integration/repo`:
   - `git clone -c core.longpaths=true --no-local` of the template's source repo, read only;
   - fetch exactly that ref from the predecessor's owned clone and verify it equals X;
   - detach at X.
4. **Write the oracle files** (new or exact-replace only; `oracle_conflict` otherwise). Commit with a **fixed** identity and date, so re-materializing gives the same base sha (tested), on `plan/<root8>/<key>` in the **owned** repo only.
5. **Resolve the frozen spec** = template + `source{repo: <integration repo>, anchorCommit: X, taskBaseCommit: base}`. It is written exclusively, alongside `provenance.json` (recipe sha, predecessor binding, oracle pins, base, resolved spec sha).
6. **The existing P1 path applies unchanged:**
   - ancestry (X is an ancestor of the base);
   - a NEW node run root;
   - preflight (anchor..base == exactly the oracle files);
   - the attached pilot with `claim_check` (the node value pin and the predecessor artifact);
   - the spec verifier.

## 4. Tests (`tests/test_plan_run_d3.py`, 8) and the demonstration

- **Zero-operator chain:** one invocation, a then b, ends `all_done`.
  - The resolved base's parent is X, and the diff is exactly `A other.test.ts`.
  - The provenance hashes match.
  - The primary repo is unchanged.
  - Re-materializing elsewhere gives the **same** base sha.
- **Replace (M):** an exact expected old blob leads to `all_done`; the base diff is `A other.test.ts`, `M value.test.ts`.
- **Conflict:** a "new" file that exists, or a wrong expected old blob, gives `oracle_conflict` with no clone or claim of b.
- **Tamper:** a changed recipe or changed oracle bytes gives `recipe_tamper` before any clone. A predecessor ref moved off X gives `artifact_unavailable`.
- **Import:** a recipe node with two predecessors is refused, and so is a template carrying a base or an oracle pin that does not match the template. Nothing is written.
- **P1 regression:** `tests/test_plan_run.py` gives 27 passed.
- **Demo:** `uv run python tests/demo_plan_run.py --recipe` prints `ONE run, no operator step: all_done [a ran accepted, b ran accepted]` and ends RESULT PASS. Without `--recipe`, the P1 manual flow is unchanged.

## 4a. Review fixes (ChatAgent review msg 1866; root msg 1860)

- **R1, same-repository lineage (blocking, fixed).** v1 derives a base only within ONE original repository.
  - The recipe's `template.source.repo` must equal the predecessor's **original** repository: its pinned spec's `source.repo`, or, for a recipe predecessor, that predecessor recipe's `template.source.repo`, never its integration clone.
  - The comparison is `realpath` plus `normcase`. A mismatch is the typed stop `repo_lineage_mismatch`, raised **before** binding, any clone or any claim.
  - Without this, a recipe naming another repository would run its frozen template against the predecessor's tree.
- **R3, test gap (fixed).**
  - A three-node chain a → b (recipe) → c (recipe) now runs to `all_done` in one invocation. c binds b through b's **resolved spec plus provenance**, and the a → b → c ancestry is checked.
  - If b's provenance no longer names b's recipe pin, the stop is `predecessor_provenance`.
- **R4 (fixed).** A corrupt or unreadable predecessor record (evidence, provenance, resolved spec) is the typed stop `predecessor_evidence` instead of the generic `unexpected_error`.
- **R5 (accepted as is).** More than one run-owned ref at the same commit is a conservative `artifact_unavailable`, which fails closed.

## 4b. Limit (R2): a recipe's baseline is predicted before the predecessor exists

A recipe freezes its baseline cases and verify steps **before** the predecessor's artifact X exists.
- **Covered:** preflight re-proves the structured baseline at the derived base before any spend, so a mispredicted baseline is a typed preflight stop, not a wasted model run.
- **Not covered:** the satisfiability proof required before a freeze (msg 1845) cannot run against the real X. A contradiction that only X introduces, for example an assertion of the old contract added by the predecessor, shows up only at verification, after model spend.
- **For the eventual authoring flow:** a recipe's satisfiability proof is made against a **reference** predecessor artifact, and that reference is recorded with the recipe.

## 5. Not in v1

- merges (more than one predecessor);
- automatic integration into the primary repo, or a push (the final chain result stays a reviewed manual integration);
- retries;
- a persistent store (P2, plan 043).
