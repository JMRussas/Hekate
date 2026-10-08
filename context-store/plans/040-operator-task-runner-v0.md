# Plan 040 — operator task runner v0 (supervised-task-spec.v0)

**Status: implemented, pending independent review (2026-10-07).** Branch `feat/operator-task-runner` from `4d61d3e`. GO: bridge msg 1632. Spec shape: frozen at msg 1636/1650. Pre-freeze review fixes: msgs 1659/1660 (F1–F5) and 1653. **No real npm, preflight or model run has happened.** The real CA012 worker run needs a separate root GO after review.

This runner drives the **existing** pilot (`e1/pilot.py`), the real-worker adapter (`e1/cli_worker.py`) and the verifier pattern from `e1/pilot_real.py` from **one closed task spec**. It is not a second engine and has no workflow DSL. Every command is a literal argv whose program is the hash-pinned Node. The first spec is ChatAgent CA012, `tests/fixtures/ca012-spec-v0.json`, sha256 `b919504a353bbc21c730caedbbab244c2e457e2ef2d249c5456d91b60c322d4e`.

## 1. The spec (`e1/task_spec.py`)

`supervised-task-spec.v0` is loaded strictly and validated key by key before anything happens. Loading is strict: UTF-8, no duplicate keys, no NaN, no floats anywhere, at most 64 KiB. Any deviation is a typed `SpecRefused`.

| Key | Rule |
|---|---|
| `source` | Absolute `repo`; `anchorCommit` ≠ `taskBaseCommit`, both 40-hex. |
| `allow` | 1–8 entries, each exactly `M` / `100644`. Never an oracle file, `package(-lock).json`, `npm-shrinkwrap.json`, a dot-file, or anything under `node_modules/` or `.git/`. |
| `oracle.files` | 1–8 `{path, sha256}`. |
| `oracle.baseline` | `{argv, timeoutS, reportMaxBytes, expectedExit, cases[{file, fullName, status, failureFirstLine}]}`. Cases are unique, `file` is an oracle path, and a passed case has a null first line. |
| `verify.steps` | 1–4 steps with unique names; exactly one is named `oracle`. |
| argv (all) | `argv[0]` == `hashes.pinnedNodeExe.path`. Every other element is a literal `[A-Za-z0-9._/:=-]`. An operand, **or the value after a flag's `=`**, that looks like a path is normalized and repo-relative. No element has a `..` segment or an absolute or drive path anywhere (F2). |
| `worker` | `testCommand` == the oracle argv joined by single spaces. Model, budget (> 0, two decimals), `maxRounds` 1–2 and `maxTurns` 1–60 are in the adapter's bounds. |
| `hashes` | Node and npm-cli `{path, version, sha256}`; lockfile, vitest and tsc entry sha256. |
| `deps` | `npm-ci`; `prefer-offline` or `offline`. |
| `metadata` | Declared and never interpreted. |

## 2. Commands (`e1/task_runner.py`)

| Command | Effects |
|---|---|
| `plan` (default) | **None.** It validates and prints the summary: no clone, install, test or spawn. |
| `preflight --run-root R` | Everything before model spend. Writes `R/preflight.json` (ok, or the typed refusal). |
| `run --run-root R --exe … --exe-sha256 … --launch-real-model --root-go …` | Needs a passing preflight of the **same spec sha256** in `R`, then runs one supervised pilot. |

**Preflight steps:**
1. Pins: Node and npm-cli by sha256.
2. Owned clone: `git clone --no-local --no-checkout` of the source (read only), then a detached checkout at the base. Git runs under `git_env()` throughout.
3. Anchor: the anchor is an ancestor of the base, and `anchor..base` touches **exactly** the oracle files (A/M, new mode 100644). Each oracle blob has its declared hash.
4. Dependencies: the lockfile hash is checked before install, in a pristine `node_modules`. Then `npm ci --ignore-scripts --no-audit --no-fund --offline|--prefer-offline` runs under the pinned Node with `run_bounded` (tree kill, drained). The vitest and tsc entry hashes are checked after.
   - **npm's environment** (revision 2, msgs 1675–1683, after the first real preflight was refused at `deps_install_failed`):
     - npm, and only npm, gets the worker allowlist plus three variables. The worker and test environments are not widened.
     - `npm_config_cache` is the operator's warm cache. It is required and must be an absolute, existing directory under native `Path` rules, so a relative path, a drive-relative `C:x` or a missing UNC path is refused. It fails closed: `npm_cache_required` / `npm_cache_invalid`, with no silent default cache.
     - `npm_config_userconfig` and `npm_config_globalconfig` point to **two distinct**, owned, **empty** files in the run root: `npm-empty-user.npmrc` and `npm-empty-global.npmrc`. The real npm 11 refuses one file as both ("double-loading config", msg 1689). Preflight creates both exclusively, outside every worktree, and each is re-checked as an empty regular file before every npm spawn (`npmrc_missing` / `npmrc_not_empty`).
     - An opt-in check (`HEKATE_E1_INTEROP_LIVE=1`) runs the real pinned `npm config get cache` under `npm_env()`, with no network and no install, and asserts that it prints the forwarded cache.
     - npm's builtin `npmrc` beside the pinned CLI cannot be disabled; the env values override it. Its sha256 is recorded as provenance.
     - The deps evidence records the cache path, whether `_cacache` is present, the npmrc path and hash, and the **bounded output head** on success and refusal. No config contents or credentials are recorded.
5. Structured baseline: the run must end normally, its report must not be truncated, and its exit must equal `expectedExit`. The report is **stdout only**; stderr is bounded, digested and kept as evidence (F1). The set of `(file, fullName, status, first failure line)` must equal the declared cases. No suite-level `message` is allowed, and the counts must agree. A mismatch gives `spec_baseline_mismatch` with the missing and extra cases.

**Run:**
- The preflight repo is bound to `R/repo`; a recorded path is never trusted (F3).
- The worker gets its own dependencies through the adapter's new optional **`prepare(worktree)` hook**. It runs after `worktree add` and the HEAD check, and before `launch_intent`. If it fails, the result is `prepare_failed`: nothing is journaled and nothing is spawned.
- Evidence is best-effort per part (`errors[{part, type}]`), and `evidence.json` is always written. It keeps `ownedRefs`.
- Nothing is integrated or pushed.

## 3. The verifier (`SpecVerifier`)

Decision order. **uncertain** means the run stops for an operator, with no decision and no retry.

1. **View binding:**
   - the view is verified, its candidate digest matches, and its artifactRef equals the order's artifact; otherwise uncertain;
   - the artifact's parent is the task base; otherwise uncertain.
2. **Before any artifact code runs:**
   - the raw diff is non-empty and contains **only** allowlisted `100644→100644 M` records; otherwise rejected (`diff_outside_allowlist`);
   - the oracle blobs **at the artifact** are unchanged; otherwise rejected.
3. **Worktree:** a fresh worktree is made at the artifact, and its HEAD must equal the artifact (uncertain otherwise). A **fresh** `npm ci` runs after the worker has exited; if it fails, the result is uncertain.
4. **Before every step:** `check_pins` (Node and npm) and `check_tree` re-hash the oracle files, the lockfile and the vitest/tsc entries on disk. If either fails, the result is uncertain (`integrity_before:<step>`).
5. **Running steps:**
   - an unconfirmed kill or an undrained reader is uncertain;
   - a timeout is rejected;
   - a non-oracle step that exits nonzero is rejected.
6. **The `oracle` step:** its stdout is the report.
   - If the report is truncated or not JSON, the result is uncertain.
   - A complete report with a nonzero exit is rejected.
   - Exit 0 is accepted only when:
     - the reported `(file, fullName)` set is **exactly** the declared set (F4);
     - every case passed;
     - `numFailedTests` is 0;
     - no suite error is reported.
7. **After all steps:** `git status --porcelain --untracked-files=all --ignored=matching` must be exactly `["!! node_modules/"]` (otherwise rejected), and `check_tree` runs again (uncertain if it fails).

## 4. Limits (not claimed)

- **No OS isolation.** The worker, npm and the steps run as the operator user. Install scripts are disabled, but dependency code still runs during steps.
- The lockfile pins what is installed; `prefer-offline` may fetch from the registry (the network fallback is authorized, msg 1632).
- Merged output is kept only for non-report steps; the oracle and the baseline split stdout from stderr.
- v0 allows only `M` of existing regular files: no adds, deletes, renames or mode changes.

## 5. Tests (offline)

- `tests/test_task_spec.py`: the frozen CA012 file validates byte-exact, every deviation gives a typed refusal (including the F2 flag-value cases), and in-repo flags and operands still validate.
- `tests/test_task_runner.py` runs on a fixture `c0 → anchor → base` repo. Its fakes are a pinned "node" (the base Python through an underscore-free junction) and `tests/fake_node/{npm_cli,vitest,tsc}.py`; the fake vitest always writes a Node-style stderr warning. Coverage:
  - plan has no effects;
  - typed preflight refusals;
  - the recorded CA012 baseline report equals the frozen spec's cases;
  - each verifier outcome;
  - re-checks before every spawn;
  - the preflight gate;
  - fake-CLI end-to-end runs (accepted; a fix round; oracle or lock edits rejected before any artifact code; a failed prepare spawns nothing; best-effort evidence) and `main` preflight → run.
- `tests/test_cli_worker.py`: prepare ordering, failure and default-None behaviour.
- The fixtures `tests/fixtures/ca012-*.json` are `-text` in `.gitattributes`.
