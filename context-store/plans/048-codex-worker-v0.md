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

## 5. Known limits

- Windows PowerShell 5.1 decodes UTF-8 files as ANSI for shell reads (smoke-004 mojibake); Codex edits through its own
  patch tool, so edits are expected to be unaffected. The first real run will show it.
- Real-run verification is a separate root GO: one rehearsal of the frozen README spec with `--worker codex`.
