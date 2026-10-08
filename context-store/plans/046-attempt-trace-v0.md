# Plan 046 — attempt trace v0: each node attempt's visible conversation

**Status: integrated and exercised live (2026-10-08).**
- **Commits:** capture A `dcb45b0` (root review 2452), integrated with the UI (`7641313`) in `3ba10b7`.
- **Api:** B/C is plan 047 (`a15f5d9`, `a3c3ba0`).
- **UI:** central trace layout `7fc017f`, and `1e98805` for permission_denied detail.
- **Live runs:** the full path (capture → Api → Plans UI) ran in two rehearsals, each with a GET-only
  capture and verified integrity:
  - Claude `readme-trace-001`: 37 records, accepted;
  - Codex `codex-trace-001`: 12 records, accepted by the independent verifier while its shell
    commands failed on stderr; see plan 048.
- **Branch:** `feat/attempt-trace-capture` from `d7b51e2`.
- **GO:** root msg 2441 (capture A), early review 2452 (R1/R2), contract `trace-contract-001` rev 2 (root review 2450).
- **Implementation test scope (before live rehearsals):** the fake CLI and temporary repos. No model, no live DB, no push, no edits to the primary repo.

## 1. What changes for the user

A node attempt is no longer a black box between `launched` and `exited`. Each round keeps the exact prompt and an ordered
record of what the worker CLI printed, so a UI can show the attempt's prompt, assistant messages, tool calls and results,
and the CLI's own stderr. It is per attempt, not a persistent repo chat. Acceptance is unchanged: the verifier still
decides, and a worker's own "success" or exit 0 never means accepted.

## 2. Files (in the pilot run dir, beside `run.json`; N = round)

| File | Content |
| --- | --- |
| `attempt-rN.prompt.txt` | the exact bytes written to the worker's stdin |
| `attempt-rN.trace.jsonl` | one JSON record per line, in receipt order across stdout and stderr |

Both are created exclusively; an existing file is never overwritten (`trace_setup_failed`, nothing journaled, nothing
spawned). Each record is ASCII-escaped JSON (lossless for any text, so encoding can never fail):

```json
{"seq": 0, "tMs": 12, "stream": "stdout|stderr|hekate", "text": "<one line, no newline>", "cut": false, "redacted": false}
```

- **stdout:** each line the worker printed, retained within the adapter's EXISTING caps (`stdout_lines_max`,
  `stdout_bytes_max`; an over-long line already kills the run). Invalid UTF-8 becomes U+FFFD.
- **stderr:** split into lines. Retention is bounded at intake by the existing `stderr_keep` RAW bytes, delimiters
  included; the bounded prefix is kept (a partial line at the cap as one `cut` record). A line longer than 256 KiB is kept
  once as `cut`. The full-stream stderr digest and byte count in `exited` are unchanged and describe ALL stderr.
- **Private reasoning is never retained:** Claude `thinking` / `redacted_thinking` content blocks and Codex `reasoning`
  item text are dropped; the record is re-serialised with `redacted: true` and keeps every visible block.
- **hekate notes** (fixed texts only): `stdout_cap_reached`, `stderr_cap_reached`, `stdout_line_over_cap`,
  `killed:<reason>`, `exit:<code>`, `spawn_failed`, `trace_incomplete:<why>`.
- A retention failure never changes the worker's protocol processing or the round's outcome.

## 3. Journal and evidence

`launch_intent.data.trace`, written BEFORE spawn so a live, killed or aborted attempt can be found:

```json
{"version": "hekate-attempt-trace.v0", "executionKind": "claude-cli", "runDir": "<absolute pilot run dir>",
 "prompt": "attempt-rN.prompt.txt", "trace": "attempt-rN.trace.jsonl"}
```

`exited.data.trace`, frozen before the record is written (later writes are ignored, so the hash is the file's):

```json
{"prompt": {"bytes": 0, "sha256": "…"},
 "trace": {"bytes": 0, "sha256": "…", "records": 0, "capped": false, "writeError": false},
 "complete": true}
```

- The trace hash describes exactly the retained file bytes; it is not the full-stream stderr digest.
- `complete` is false when a write failed (`writeError`) or on the abort path.
- **Abort path:** when a journal callback fails, no `exited` is written (unchanged). The trace is still finalized with
  `complete: false` and the notes `killed:supervisor_error`, `trace_incomplete:supervisor_error`, into
  `evidence.json` `adapter.<round>.trace = {ref, final}`.
- Every round's `adapter.<round>.trace` repeats `{ref, final}`.
- Runs made before this change have no `trace` block (the Api reports them as `not_captured`). `evidenceRef` keeps its
  meaning; no historical journal or evidence is rewritten.

## 4. Changed code

- `e1/cli_worker.py`: `AttemptTrace`, `redact_reasoning`, `trace_names`; the readers feed the trace in receipt order
  (stderr now reads with `read1`, so lines arrive as written; its full digest is unchanged); `CliConfig.execution_kind`
  (validated against `pilot.EXECUTION_KINDS`, a label only).
- `e1/pilot_real.py`, `e1/task_runner.py`: pass the run's execution kind to the adapter.
- `tests/test_attempt_trace.py`, `tests/fake_cli.py` (trace scenarios).
- No frozen or revised module pin changes (`cli_worker.py`, `pilot_real.py`, `task_runner.py` are not pinned).

## 5. Next (not in step A)

- **B/C:** `GET /api/plan-contract/v1/nodes/{nodeId}/attempts/{attemptId}/trace`, read-only, per contract
  `trace-contract-001` rev 2 (identity through claim receipts and the journal, path confinement, integrity, paging).
- **E:** the Codex worker backend (feasibility: smokes 001–004; the WindowsApps `pwsh` alias must be excluded from the
  worker's PATH under the unelevated Windows sandbox).
