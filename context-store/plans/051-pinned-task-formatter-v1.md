# Plan 051: Pinned trusted supervisor formatter (supervised-task-spec.v1)

**Status: independently reviewed and verified locally; live native UI model run remains the next rehearsal.** LOCAL development only. Task `db59fb7b-d963-537a-853a-8883601ab757` (CA-ISSUE-016). Modules: `e1/task_spec.py` (v1 shape), `e1/task_format.py` (new), `e1/cli_worker.py` (`finalize` hook), `e1/task_runner.py` (wiring, preflight pin check), `e1/task_author.py` (profile/draft v1); tests: `tests/test_task_format.py`.

## 1. Purpose

A worker whose only Bash command is the frozen oracle cannot run Prettier, so an otherwise-correct candidate can fail a UI repo's format check (the recorded native pilot's two rejected formatting rounds are preserved and unchanged). v1 lets a spec name ONE trusted formatter that the **supervisor** runs after the worker exited and before the artifact commit. It is not a worker permission, not a shell widening and not verifier normalization.

## 2. What is unchanged

- `supervised-task-spec.v0`, `hekate-task-profile.v0`, `hekate-task-draft.v0`: same bytes, same validators (v0 with a `formatter` key is `spec_shape`), same 1..4 verify steps, same authority.
- The strict oracle, `worker.testCommand` and the restricted Claude shell. The verifier never writes and never formats; an unformatted candidate still fails a verify step such as `prettier --check`.
- `finalize=None` (every v0 run) leaves `CliWorker._capture` on its old path and adds no `finalize` key to its serialized adapter evidence.

## 3. The v1 spec

`specVersion: "supervised-task-spec.v1"` = the v0 keys plus a required `formatter`; `verify.steps` may hold 1..5 (v0: 4). Descriptor (closed, every key required):

| key                           | rule                                                                                                                                                              |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prettierEntry`               | `{path, version, sha256}`; path repo-relative under `node_modules/prettier/`, normalized, literal-argv-safe; version `d.d.d`; sha256 of the installed entry file  |
| `config`                      | `{path, sha256}`: the ONE config Prettier may read (`--config`, `--no-editorconfig`); not an allow path, oracle file or under `node_modules`; sha256 of its bytes |
| `paths`                       | 1..8 normalized, literal-argv-safe, non-flag paths, each one of `allow` (no duplicates)                                                                           |
| `timeoutS`, `outputKeepBytes` | 1..120, 1..65536                                                                                                                                                  |

There is no free-form argv: the supervisor builds `[pinned node, entry, --write, --config, <config>, --no-editorconfig, *targets]` from validated values. Node is the spec's existing `hashes.pinnedNodeExe`.

`hekate-task-profile.v1` = profile v0 plus `formatter` (`prettierEntry`, `config`, `timeoutS`, `outputKeepBytes`) and 0..4 profile steps (4 + the oracle step = 5 total, so a UI repo can require a headless browser check plus typecheck/full/docs). `hekate-task-draft.v1` = draft v0 plus `formatPaths` (a subset of `allow`). A v1 profile accepts only a v1 draft and composes a v1 spec through the unchanged `task_spec.parse`.

## 4. Where it hooks (no second engine)

`CliConfig.finalize(worktree, round)`, called by `CliWorker._capture` after the worker's exit and HEAD check and BEFORE `git add -A`/commit. `task_runner.run` installs it only for a spec with a formatter. `FinalizeRefused(status, reason, evidence)` maps to `WorkReport(failed|unknown)`; any other exception is `unknown/finalize_error`. `unknown` stops the pilot with no verdict and no model retry, exactly as other infrastructure doubt does.

## 5. Sequence (`task_format.format_candidate`)

1. Re-validate the spec. Stage and read the candidate's raw diff: it must be exactly allowlisted regular `100644` modifications, else `failed/diff_outside_allowlist` (the candidate's fault, no spawn, nothing created). This also constrains the oracle files and the lockfile, which are never allow paths.
2. Targets = descriptor `paths` that the candidate changed. None: nothing runs (an empty diff still fails later as before).
3. Keep the candidate's exact pre bytes under `<run_dir>/format-r<N>/pre/`, exclusively.
4. A FRESH owned worktree at the task base with pristine `npm ci` deps (the worker's own `node_modules` is never executed); write the targets' pre bytes there.
5. Run the bounded runner twice (format, then idempotence). Right before and after each run: Node/npm pins, `check_tree` (oracle files, lockfile, vitest/tsc entries), the Prettier entry and config sha256 (regular files, no link on the path). After each run the raw diff there must stay modifications of the targets only.
6. Second run must leave every byte as the first left it, else `unknown/format_not_idempotent`.
7. The candidate paths must still hold the pre bytes; write the post bytes (also kept under `post/`), re-check the candidate's raw diff (scoped, subset of the original changed set, bytes equal to the post bytes). The supervisor then commits as before.
8. Any failure, timeout, unconfirmed kill/drain, pin or scope doubt, or host error: `unknown/<reason>`; pre-format candidate bytes and partial evidence are kept; a failure while applying the final bytes may leave a partially derived candidate. It is never accepted or retried under uncertainty.

Evidence (`RunEvidence.finalize`, in `evidence.json` under `adapter.<round>.finalize`): `kind: host-derivation`, formatter pins, argv, each run's rc/kill/drain/output digest, per-file pre/post sha256, sizes, changed flag and file paths. It is separate from `reported_usage` (provider usage) and states `usage: none`.

## 6. Preflight

`run_baseline` also checks the Prettier entry and config pins in the baseline install (`formatter_pin_mismatch`), before any model spend. A spec whose pins do not match the repo cannot reach a run.

## 7. Limits (honest)

- Only the Prettier entry file and config are individually hashed; the rest of the fresh `node_modules` is as trustworthy as `npm ci` from the hash-pinned lockfile.
- Not OS isolation: the formatter runs as the supervisor's user in an owned worktree; no network is denied beyond what `npm ci --offline` already uses.
- Output beyond `outputKeepBytes` is digested and counted, not kept.
- Formatting happens once per round; a rejected round's retry formats again. A formatter that is correct but disagrees with the repo's verify `--check` (different config) is a rejection by the verifier, not a formatter failure.
- The full formatter suite now passes 44 cases. The preceding selected run passed 208 cases with one overly broad pin-test spawn guard failure; that guard now permits dependency installation and still forbids a tampered formatter spawn. The unchanged other 165 selected cases passed. Initial candidates and failures are retained under `D:/hekate-coordinator/runs/task-formatter-001/`.
- A real pinned Node v24.21.0 / Prettier 3.9.9 proof passed against the frozen UI base (68 cases, 63 deliberate baseline failures): recorded unformatted bytes were transformed to the exact independently reviewed reference bytes and the second run was byte-idempotent. Evidence: `D:/hekate-coordinator/runs/task-formatter-real-prettier-001/real/derivation.json`. No live native UI model run is claimed yet.
