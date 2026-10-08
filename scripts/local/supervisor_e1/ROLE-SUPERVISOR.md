# Role supervisor (one read-only role as a managed attempt)

`e1/role_supervisor.py` runs ONE injected read-only `BaseChatModel` role (`e1/role_worker.py`) under the EXISTING
`e1.supervisor.Supervisor`. It is a synchronous host callable, not a scheduler: it supplies the Supervisor's three seams
(package builder, worker, journal hook) and delegates claim, receipt/pin checks and finish to it unchanged. It returns the
Supervisor's own `SupervisedRun`.

```
uv sync --group roles
uv run --group roles pytest tests/test_role_supervisor.py
```

Without the `roles` group the test module skips itself.

## What one run does

1. **Preflight, before any claim** (constructor and `run()`): journal hook is callable (non-null), client has the four
   Supervisor methods, role/model/binding/model identity/snapshot/task UUID/source revision/`observed_at` are valid, the
   evidence identity for the manifest builds, and `run_dir` is absolute, short (<= 200 bytes), its parent exists and no
   existing path component is a symlink/junction/reparse point, and the directory does **not** exist. `run()` then creates
   it with `mkdir` (exclusive) and requires it empty. Any refusal returns `needs_operator` (`host_refused:<code>`) with no
   claim, no journal record and no model call. An instance runs once.
2. `Supervisor.run` claims (journal `claim_intent` first). A replayed claim, stale/unrecorded state or an uncertain operator
   state never reaches the model.
3. **Package builder**: claimed node must equal the expected task UUID; the plan node must still be `in_progress` for the same
   attempt/epoch; the actual current node `stateRevision` is read (never an epoch or event seq). The pinned task content
   (`receipt.content_snapshot_norm`) plus receipt identity is appended to a copy of the host snapshot as the reserved evidence
   item `hekate-claim-receipt` (a host item with that id is refused). Refusal stops before dispatch.
4. **Journal adapter** (wraps the hook): `launch_intent` is enriched with the native trace ref
   (`executionKind: "langchain-role"`, `inProcess: true`, `trace: {version, executionKind, runDir, prompt, trace}`) and the
   `modelOnly` flag is dropped; the trace files are created before the record is written. The host records `launched`
   (invocation start, no PID, no CLI) just before the model call; the Supervisor's later model-only `launched` is swallowed by
   returning that receipt. `exited` carries the final trace metadata, exit code and reason; `result_captured` carries
   refs/hashes only (`candidate` is false for any failure). Exactly one of each record per invocation. A refused or
   degraded intent or `launched` stops before the model; a failed/degraded `exited`/`result_captured` stops before finish.
5. **Worker**: one `run_role` call. On validated output it writes `role-result.json` (exclusive), closes the trace, builds
   `role-manifest.json` with `role_evidence.build_manifest` (prompt, trace, result; `linkage: host_asserted`), re-reads every
   file from disk and verifies the manifest against the expected identity, and re-reads the plan node: `stateRevision`
   must be unchanged. Only then does it return a successful `WorkResult` whose `artifact_ref` is
   `role-manifest:sha256:<digest of role-manifest.json>`.
6. The Supervisor re-reads the receipt/plan and finishes to `done` (awaiting independent review). There is no accept/review
   write anywhere in this module or in the client it is given.

Exit codes in `exited`/`WorkResult`: `0` success, `2` typed role failure (invalid/refused output, outage, deadline), `3` host
failure (journal/file/trace/manifest/state). A failure or incomplete/capped trace is never a success and never finishes.

## Files (in `run_dir`, all exclusive-create)

| file | content |
| --- | --- |
| `attempt-r1.prompt.txt` | `[system]\n<role system prompt>\n\n[user]\n<canonical snapshot incl. reserved item>` (exactly what the model gets) |
| `attempt-r1.trace.jsonl` | `AttemptTrace` records: host stage notes, the validated output (stream `stdout`) or a typed failure note |
| `role-result.json` | canonical `hekate-role-result.v1` (output, `reviewStatus: pending`, `accepted: false`, correlation, hashes) |
| `role-manifest.json` | canonical `hekate-role-evidence.v0` manifest over the three files above (retained path = this file) |

The prompt/trace names are the viewer-compatible `AttemptTrace` names (round 1). `trace` in `launch_intent` is the same
`AttemptTrace.ref()` block the CLI worker writes, so the native trace linkage works unchanged.

## Example (offline, injected fake model)

Run from `scripts/local/supervisor_e1/tests` (it reuses the test fakes: a fake client serving the real claim fixture, the
in-memory `ModelJournal` and a fake `BaseChatModel`):

```python
import json, tempfile
from pathlib import Path
from e1 import role_supervisor as RS, role_worker as RW
from test_role_supervisor import FakePlan, Hook, HookChat, NODE, ROOT, KEY, ATT, EXEC, SRC, OBSERVED
from test_role_worker import GOOD, SNAPSHOT

base = Path(tempfile.mkdtemp())
host = RS.RoleSupervisor(
    client=FakePlan(), journal=Hook(), role=RW.athena_role(), model=HookChat(reply=json.dumps(GOOD)),
    snapshot=SNAPSHOT, task_id=NODE, source_revision=SRC, observed_at=OBSERVED,
    run_dir=base / "run", model_identity="fake-model-1")
run = host.run(ROOT, KEY, ATT, EXEC)
print(run.outcome, run.reason, run.result.artifact_ref)     # Outcome.FINISHED finished role-manifest:sha256:...
```

## Live integration recipe (read-only role, local state)

Reuse the existing local PlanStore client and durable journal; nothing new is created for them:

```python
journal = DurableJournal(dsn, writer).open()                     # e1/durable.py: fenced single writer
host = RS.RoleSupervisor(
    client=SupervisorClient("http://127.0.0.1:<port>"),          # the real Supervisor client (claim/finish/release only)
    journal=journal.hook(root, claim_key),                       # already bound to (rootId, claimKey)
    role=RW.athena_role(), model=<host-built BaseChatModel>, model_identity="<host record>", provider="<host record>",
    snapshot=<immutable evidence snapshot dict>, task_id=<expected task UUID>, source_revision=<40-hex git revision>,
    observed_at=<RFC 3339 with offset>, run_dir=<fresh absolute path, parent exists>)
result = host.run(root, claim_key, attempt_id, executor_ref)
```

`claim_key` must also be a valid manifest id (`[A-Za-z0-9][A-Za-z0-9._:@+-]*`; PlanStore keys containing `~` are refused
before the claim). The `Supervisor` is the only writer of claim/finish; the lead should inspect the journal stream and the
files in `run_dir` afterwards.

## Limitations

- `host_asserted` linkage and same-machine sha256 only: nothing authenticates an actor or proves the model/provider, and
  `model_identity`/`provider` are the host's own words.
- **No secret scrubber.** Snapshot, prompt, pinned task content, output and trace are retained verbatim in `run_dir`
  (`restricted_raw`); keep the directory private. Raw provider responses and private reasoning are not retained (the trace
  holds stage notes plus the validated output or a typed failure code; model exceptions are reduced to a code).
- A deadline/stop cancels the awaiting task cooperatively; it is **not** a rollback and cannot prove the provider stopped.
- Interruption (`KeyboardInterrupt`/cancellation) closes the trace incomplete and re-raises; the attempt stays in progress,
  with `launch_intent`/`launched` recorded and no `exited`. Nothing is released, retried or resumed automatically.
- No unattended resume, no scheduler, no acceptance or review write, no legacy DB projection, and no production
  authentication claim. Failure of any step leaves `needs_operator` with all evidence retained.
- Synchronous only: it refuses to run inside a running event loop (it uses `asyncio.run` for the single model call).
- The manifest's `stateRevision` is the node revision read before the model call; if the node changed meanwhile the run is
  not finished (`state_changed`).
