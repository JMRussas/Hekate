# Task authoring v0 (`e1/task_author.py`)

Writes ONE pinned `supervised-task-spec.v0` from a short draft and a reviewed target-repo profile, and proves it before calling it ready. It launches nothing: no worker, model, harness, PlanStore or plan run.

```powershell
cd scripts/local/supervisor_e1
$env:npm_config_cache = "D:\npm-cache"     # a warm cache: dependencies install with npm ci --offline only
uv run python -m e1.task_author draft --draft D:/plans/task.draft.json `
  --profile D:/Git/ChatAgent/docs/contracts/hekate-task-profile.json --reference D:/plans/task.reference.patch --out D:/plans/task-001
```

## Inputs

- **Profile** (`hekate-task-profile.v0`, reviewed and kept in the target repo):
  - the repo, plus the trusted tool pins (`pinnedNodeExe`, `npmCli`) and lock pins (`packageLock`, `vitestEntry`, `tscEntry`);
  - offline `deps`;
  - the `oracleRunner` argv, with `{oracle}` exactly once;
  - the other verify `steps` (at most 3) and the default `worker` bounds.

  A lockfile change needs a reviewed profile update: until then a draft is refused with `profile_lock_mismatch`.
- **Draft** (`hekate-task-draft.v0`, the judgment fields only): `source` {anchorCommit, taskBaseCommit}, `task` {text, criteria}, `allow` (paths), `oracle` (paths), `worker` (optional overrides of model, budgetUsd, maxRounds, maxTurns) and `metadata`.
- **Reference** (`--reference`): a patch that implements the task. It is only for the satisfiability proof: it is kept under `evidence/` and never enters the package or a worker prompt.

## What it checks

**Refused before any effect** (exit 2, no output folder). Each check names its refusal:
- the inputs are strict, and each is at most 64 KiB;
- the tool pins are re-hashed before any subprocess;
- the base's lock blob equals the profile pin;
- every oracle file exists at the base;
- the composed spec passes the unchanged `task_spec.parse`.

**Then, in the new `--out` folder** (exit 1 means NOT READY, and the evidence is kept):
1. **Capture.** A labelled provisional spec (`evidence/capture/capture-spec.json`; never a task spec) runs the oracle at the base through the existing `run_baseline`.
   - The report must be normal: no suite-level error, and every failure an `AssertionError`.
   - At least one case must fail.
2. **Final spec.** The captured cases go into `evidence/spec.json`, which the unchanged `task_spec.load` reads.
3. **Preflight.** The unchanged `task_runner.preflight` runs in `evidence/proof/`.
4. **Reference.** The patch is committed on the base in that owned clone, and the real verifier (`SpecVerifier.check_artifact`) must accept it. This covers the oracle, every step, the allow list and a clean tree.

**Ready** (exit 0): only then is `package/spec.json` written, with the same bytes. `package/` exists only for a ready package.

`authoring.json` is written last in every outcome. It holds:
- the input sha256s (draft, profile and reference);
- the capture, preflight and reference results;
- `ready`, or `notReady` {stage, code, detail}.

**Edge cases** (reviews 2002/2005):
- Capture accepts only `AssertionError` failures. An oracle that imports a function that does not exist yet fails at the base with `TypeError: x is not a function` and is refused (`baseline_not_assertion`). Make it fail by assertion first, for example `expect(typeof mod.fn).toBe("function")`.
- The reference patch, like the draft and the profile, is at most 64 KiB (`reference_size`).
- An unknown anchor or base commit is `anchor_not_found` / `base_not_found`.
- An unforeseen error BEFORE the folder exists (for example a git timeout) is refused as `pre_effect_error` (exit 2, nothing created). After the folder exists it is a typed not-ready (`unexpected_error`), with the evidence kept. Both report only the error's type name.
- If `authoring.json` itself cannot be written, the result is exit 1 with `recordFailed` (`authoring_record_failed`), never "no effects". **A package counts as ready only when `authoring.json` exists and says `ready: true`.**

Running the package is a separate, reviewed step: `plan_cli` with its own GO.

Limits (v0): one pinned task only. There are no recipes or multi-task plans, no task or oracle generation, and only vitest/tsc projects (the runner's fixed entries).

Tests: `uv run pytest -q tests/test_task_author.py` (offline: fake node, npm, vitest and tsc).
