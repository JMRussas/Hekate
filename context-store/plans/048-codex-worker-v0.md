# Plan 048 — codex worker v0: `codex exec` as an alternate node worker

**Status: implemented, pending root review (2026-10-08). No model was run during implementation.**
- **Branch:** `feat/codex-worker` from `dcb45b0` (plan 046 capture A).
- **GO:** root msg 2469 (proposal 2468). Feasibility: smokes 001–004 and the model-free sandbox diagnosis (msgs 2427–2446).
- **Test scope:** a fake codex (`tests/fake_codex.py`) replaying the smoke-004 event shapes, temporary repos, the
  disposable harness. No model, no network, no push.

## 1. What changes for the user

A plan can run its nodes with the Codex CLI instead of Claude Code, from the command line, with the same plan, spec,
verifier and attempt trace:

```
python -m e1.plan_cli run --plan <plan.json> --run-root <new dir> --worker codex \
  --exe <native codex.exe> --exe-sha256 <its sha256> --launch-real-model --root-go <GO> [--worker-model <model>]
```

`--worker` defaults to `claude`, which is unchanged. The same flags exist on `task_runner run`. Spec and plan bytes are
unchanged: the backend is an explicit run override (a node-owned spec field is a later schema revision).

## 2. What is recorded truthfully

| Question | Codex answer |
| --- | --- |
| Model | The spec's `worker.model` is a Claude alias and is NOT passed (`specModelIgnored`). `--worker-model` is passed as `-m`; without it the CLI's own default is used (`requestedModelSource: cli-default`). Codex reports no model: `reportedModel: unreported`. |
| $ budget, turn limit | NOT enforced by the CLI (`budgetEnforced: false`, `turnsEnforced: false`). Only the supervisor's timeouts, output caps and tree kill bound the run. |
| Usage | Tokens only, from `turn.completed.usage` (`input`, `cached_input`, `cache_write_input`, `output`, `reasoning_output`); cost, turns and duration are null. Nothing about billing is inferred. |
| Shell scope | Any shell command inside the workspace-write sandbox. Claude's `Bash(<testCommand>)`-only allowlist has no codex equivalent; the test command is named in the task, not enforced. |

These terms are in `launch_intent.data.worker`, in `evidence.json` `worker` (both backends), and summarised in the
plan CLI result for a non-default backend.

## 3. How it runs

argv (smoke-004's, with a writable workspace for the edit; the prompt goes to stdin):

```
codex.exe exec --json --ephemeral --ignore-user-config --ignore-rules --sandbox workspace-write --color never
  -C <owned worktree> -c approval_policy="never" -c windows.sandbox="unelevated" [-m <model>] -
```

- The user's `config.toml` (its MCP servers and credentials) and execpolicy rules are not loaded.
- Environment: the same allowlist, with the exact `%LOCALAPPDATA%\Microsoft\WindowsApps` PATH entry removed for codex
  only. Its `pwsh.exe` alias cannot start under the unelevated sandbox; without it Codex uses System32 PowerShell.
- Never `--dangerously-bypass-approvals-and-sandbox`, `--approve-for-me` or codex's own `--worktree`.

Event semantics:
- **Acts:** `HEKATE-ACT` lines only from `item.completed` with `item.type: agent_message`. Command output and file
  changes never count.
- **Terminal:** exactly one `turn.completed` means the TURN ENDED. It only permits the supervisor's capture checks;
  `turn.failed` or `error` fails (`result_error`), none is `no_result`, two are `ambiguous_result`, and a turn with no
  edit fails `empty_diff`. Acceptance is the verifier's alone.
- `run.json` `dryRun` is false for `codex-cli` (it was derived as "not claude-cli").

## 4. Changed code

- `e1/cli_worker.py`: `CliConfig.backend`, `codex_command`, `command_for`, `worker_env(backend)`, `codex_act_lines`,
  `codex_terminal`, `codex_usage`, `codex_worker_terms`; the supervise loop dispatches by backend.
- `e1/pilot.py`: `EXECUTION_KINDS` + `codex-cli`, `REAL_KINDS`; `dryRun` derived from `REAL_KINDS`.
- `e1/export.py`, `e1/pilot_export.py`: accept `codex-cli` (it was labelled `fake`).
- `e1/pilot_real.py` (`adapter_config`), `e1/task_runner.py` (`worker_choice`, `worker_record`, flags),
  `e1/plan_run.py`, `e1/plan_cli.py` (flags, refusals before any effect).
- No frozen or revised module pin changes: none of these modules is pinned (checked by sha256 search).

## 5. Shell discovery fix: packaged PowerShell (2026-10-08, root GO 2512)

**Observed.** The first real rehearsal (`codex-trace-001`, root `d1e7b47e…`, accepted in round 1) ran Codex from a
PowerShell-launched environment. Its PATH held BOTH the `%LOCALAPPDATA%\Microsoft\WindowsApps` alias and the packaged
Store PowerShell 7 directory `C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe`. The v0 filter
removed only the alias, so Codex chose the packaged `pwsh.exe`, and every shell call failed:
`CreateProcessAsUserW failed: -1073283067` (0xC0070005, access denied), on stderr only. Codex still made the README
edit through its patch tool (real `file_change` items), and the verifier accepted it. smoke-004 had passed only because
its Git Bash launch PATH had no package directory.

**Fix.** `codex_path_excluded` drops the alias directory and `%ProgramFiles%\WindowsApps` together with everything
under it, for codex only; all other entries keep their order, and Claude's PATH is unchanged. Codex then falls back to
System32 Windows PowerShell 5.1.

**Proof (codex-smoke-005, model run, launched from PowerShell).** The launch PATH had 29 entries, 2 excluded; the
worker PATH had 27. Events:
- `command_execution` `"C:\windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Command 'Get-Content -Encoding UTF8
  -TotalCount 5 -LiteralPath README.md'`, exit 0, correct UTF-8 output (the em dash intact);
- `file_change` add `SMOKE.md`, with bytes exactly `codex smoke 005\n` and no BOM;
- `turn.completed`; stderr empty.
The worktree's only change was `?? SMOKE.md`. Evidence is in `D:/hekate-coordinator/codex-smoke-005/`.

## 6. Known limits

- Windows PowerShell 5.1 decodes BOM-less UTF-8 as ANSI unless `-Encoding UTF8` is passed. The codex-only prompt line
  (041b069) asks for it, and smoke-005 obeyed it with correct output. That is model compliance, not enforcement.
- Edits go through Codex's patch tool. codex-trace-001 (update) and smoke-005 (add) both produced real `file_change`
  items and BOM-less files.
- After this fix, a rehearsal of the frozen README spec with shell reads working has not yet been run. It needs its own
  root GO.
