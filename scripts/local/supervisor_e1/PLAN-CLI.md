# Prepared-plan CLI (`e1/plan_cli.py`)

One entrypoint that feeds a **prepared** `plan-import.v0` file to the existing plan-run v0 primitives: `plan_import`, `plan_run` and `task_runner`. It has no engine of its own. Cloning, npm, the verifier and the claim checks all stay in those modules.

It runs plans that are already written. It does not split a roadmap into tasks: every node's task spec is frozen beforehand.

**Test-scoped.** `run` uses the disposable harness only (a new database and Api, dropped at exit). It never uses a live database, never pushes, and never edits the source checkout.

## Before a run

The setup is the one maintained in [README.md, "Run"](README.md#run): uv, plus the owned `hekate-local` container started with `scripts/local/hekate-local.ps1 start`. `validate` needs nothing else. `run` also needs two environment variables:

- `npm_config_cache`: an absolute, existing, **warm** npm cache directory. Every install is `npm ci` against it: `--offline` when the spec's dependencies say `network: offline`, otherwise `--prefer-offline`. When it is absent the run refuses with `npm_cache_required`; when it is not an absolute existing directory it refuses with `npm_cache_invalid`. There is no default cache.
- `HEKATE_E1_CONTAINER_WORKSPACE=D:\Git\Hekate`: required when the CLI runs from any checkout other than `D:\Git\Hekate`, such as an approved clean worktree under `.worktrees/`. It names the owned container, and any other value is refused.

## Commands

```powershell
cd scripts/local/supervisor_e1
$env:npm_config_cache = "D:\npm-cache"                 # your warm cache (absolute, existing)
$env:HEKATE_E1_CONTAINER_WORKSPACE = "D:\Git\Hekate"   # needed outside the primary checkout

# No effects: parse the plan and load and hash-check every pinned spec. No harness, clone, install, test or spawn.
uv run python -m e1.plan_cli validate --plan D:/plans/slice.json

# One disposable harness: import the plan, then drive it with run_plan.
uv run python -m e1.plan_cli run --plan D:/plans/slice.json --run-root D:/runs/slice-001 `
  --exe <claude executable> --exe-sha256 <sha256 of it> --launch-real-model --root-go <root GO message id>
```

`validate` prints, for each node:
- its predecessors;
- its spec reference: `pending`, a pinned path and sha256, or a `{"recipe": {path, sha256}}` (D3 v1, plan 044);
- for a pinned spec: the issue, task base, allow list, verify steps and worker bounds (model, budget, rounds, turns). These bounds are the spec's own, applied as in the single-task runner.

## What `run` checks before any effect

Each failure below exits with code 2, before the harness starts:

| Check | Refusal |
|---|---|
| The plan parses, the graph is valid and every pinned spec loads and matches its sha256 | `plan_import`'s own code, for example `import_graph` or `spec_sha_mismatch` |
| `--run-root` is given | `run_root_required` |
| `--run-root` does not exist yet. An existing one is an earlier attempt: it is the in-flight fence, so it is never reused | `run_root_exists` |
| `--exe`, `--exe-sha256`, `--launch-real-model` and a non-blank `--root-go` are all present | `run_needs` |
| The executable exists and its sha256 matches | `executable_missing`, `executable_hash_mismatch` |

## Result

The last JSON object printed reports the result:
- `outcome`: `all_done` or `needs_operator`, with the stop `reason` and `detail` from `run_plan`;
- each node step, with its run root and `evidence.json`. In it, each round's `adapter.<round>.reported_usage` holds the CLI's own `total_cost_usd` (a decimal string), `num_turns` and `duration_ms` from the round's first result event. They are labelled `cli-reported, not metered`, and each is null when missing or malformed. They are evidence only: the budget guard is still the CLI's `--max-budget-usd`. A round whose `git worktree add` or HEAD check failed keeps `adapter.<round>.setup_error`: the step, git's rc and the last 4096 characters of its stderr, with the full length (the outcome is unchanged). `ownedRefs` is present only when git listed the refs; a failed read is recorded in `errors` with its rc and stderr, so a missing list never means "no refs"; Before the first claim, each node run also writes `provenance-<runId>.json` in its node run root, and the evidence repeats it as `provenance` with `provenanceFile` and `provenanceSha256`. It holds the git HEAD and dirty paths of `scripts/local/supervisor_e1`, the sha256 of each loaded `e1` module file, and the Python version. **It is host-observed and not authenticated: on-disk bytes at observation, not loaded bytecode, and not a no-spawn proof.** If the file cannot be written, the observation stays in the evidence, the file and hash are null, and `errors` says why; the run itself is unchanged;
- each node's final work, acceptance, blockers and artifact, from PlanStore's own view;
- the `plan-run-N.json` log path.

Exit codes: `0` all_done, `1` needs_operator (or an error after the harness started), `2` refused before any effect.

- A harness that cannot start (for example, port 5108 in use) gives `harness_unavailable`, exit 1.
- Harness cleanup problems are listed under `harness`. They do not change the exit code of an all_done run.

Failure evidence is kept: the per-node run roots, the plan-run log and, on any outcome other than all_done, the harness work folder (build output and `api.log`).

The harness database is dropped at exit **whatever the outcome**. `Harness.stop(keep_work=True)` keeps only the work folder. A failed drop is reported under `harness`.

## Limits (v0)

- **A stop cannot be resumed** with the default `--store harness`. The harness drops its PlanStore database at exit. After `needs_operator`, prepare what the stop names, then run the plan again in a **new** run root.
- **`--store local --state-dir D [--actor L]` keeps the state and continues in the SAME run root** (plan 043 rev 3 §5).
  - It opens the local coordinator's OWN marked database, named by the locator in `D`. It never creates or adopts one. A second coordinator, or a busy port, is refused.
  - **The first run** imports the plan, then binds the run root (`plan.binding.json` plus the original `plan.import.json`).
  - **A later run with the SAME plan file in the SAME run root** attaches with no re-import, skips accepted nodes, and stops on in-flight or uncertain work. An edited plan file is `plan_changed`.
  - After a `spec_pending` stop, the operator pins the spec; that is the one authorized drift. Then the same command continues.
  - Uncertain or unparseable operator-act log entries stop the run before any dispatch. This version cannot resolve them in place: never hand-edit the log, and nothing is retried.
  - Continue only in the ORIGINAL run root: a recipe node needs its predecessor's owned clone there, otherwise it stops `predecessor_evidence`.
  - A pin is trusted-local: any valid ref on a pending node is accepted; it is not cross-checked with the operator-act log.
  - `actor` is a label, not authentication. The database is never dropped.
  - Create the coordinator's own database once with `uv run python -m e1.local_cli create --state-dir D [--api-port 5109]`.
    - `D` must be outside any git work tree.
    - It refuses before any effect when `D` already holds a locator, when the port is 5108 (the harness) or invalid, or when the port is in use.
    - It never adopts, repairs or drops a database. On success it stops its Api and keeps the database.
    - A failure after the database exists keeps it and its locator for inspection (exit 1).
    - Activating a dedicated database for real use needs its own GO.
- **A recipe node needs no operator step.** A `{"recipe": ...}` node (D3 v1: one predecessor, same original repository) gets its base derived from the predecessor's accepted artifact within the same run, so a fully pinned or recipe chain can reach `all_done` in ONE `run`. Its stops (`repo_lineage_mismatch`, `recipe_tamper`, `oracle_conflict`, `predecessor_evidence`, ...) pass through as `reason` and `detail`. The integration repo, resolved spec and provenance are under `<run-root>/<key>.integration/`.
- **A pending spec is an operator stop.** A `spec: null` node, whose base is prepared by an operator (D3 v0), stops with `spec_pending` once its predecessors are accepted. Their accepted artifacts are in the per-node owned clones under the run root. To continue:
  1. integrate those artifacts into the source repo;
  2. freeze the node's spec on them;
  3. pin it in the plan file;
  4. run the plan again.
- **`--exe-arg` is for offline tests only.** It runs the fake CLI script through `--exe`, sets `executionKind` to `fake-cli` and adds a `fake` field to the result. No model runs, and nothing in that result is real-model evidence.

Tests: `uv run pytest -q tests/test_plan_cli.py`. The run-outcome cases need the owned `hekate-local` container.
