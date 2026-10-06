# Supervisor E1a — test-only plan-contract seam experiment

Plan [023](../../../context-store/plans/023-coding-worker-adapter-proposal.md) §3, first slice (E1a). Evidence: [024](../../../context-store/plans/024-supervisor-e1a-validation.md).

**This is test code, not a worker.** Nothing here launches a process, CLI or model, or edits production code.

It checks one thing: a supervisor can drive a managed leaf against the **real** plan-contract API with every write behind explicit preconditions, and every uncertain or non-success case stops for a human.

## Layout

| File | Role |
|---|---|
| `e1/exact.py` | Exact JSON (pure). Float and exponent tokens and NaN/Infinity are rejected, every integer token must fit Int64, and duplicate keys are rejected. `counter()` checks semantic counter ranges and rejects bools. |
| `e1/seam.py` | Pure validation of a **whole** raw claim response: version, flags, outcome-dependent nullability, digest forms, nested snapshots, references and counters. Also the **opaque** work package. **There is no context renderer and no package canonicalization**: the package is the token `e1-opaque:<claimKey>` plus the receipt identities verbatim. ChatAgent's H1 fields, rendering, the digest and case 16 interop are pending H1's accepted checkpoint (E1b). |
| `e1/wire.py` | Loopback-only HTTP. `SupervisorClient` exposes claim, get claim, get plan and finish/release transitions, nothing else. `SetupClient` is the test fixture's surface (create, add, revise, decide) and is never given to the supervisor. |
| `e1/supervisor.py` | `FakeWorker` (in-process, scripted) and `Supervisor` (the only writer). |
| `e1/harness.py` | A disposable database plus an Api process (port 5108). See "Isolation". |
| `tests/test_fixture_correlation.py` | Offline tests over the real raw claim fixtures in `context-store/plans/fixtures/023/`, plus derived invalid documents. |
| `tests/test_supervisor_live.py` | Live cases against the real API. |

## Run

```powershell
# The owned hekate-local container must be running (scripts/local/hekate-local.ps1 start, or docker start).
cd scripts/local/supervisor_e1
uv run pytest -q
```

uv selects CPython ≥ 3.11 (validated with 3.13.13). The only dependency is the test runner `pytest`, locked in `uv.lock`; HTTP and JSON come from the standard library.

To run from a clean checkout elsewhere, the owned container must still be named explicitly. Set `HEKATE_E1_CONTAINER_WORKSPACE=D:\Git\Hekate`; any other value is refused, and arbitrary container discovery is never done.

## Isolation

The harness owns only what it creates:
- a new `hekate_plan_e1_<utc>_<hex>` database, with a name guard, in the owned `hekate-local` container (verified by its compose and workspace labels, published on loopback only);
- one Api process, built into a private `.run/<id>/` folder and started on `127.0.0.1:5108` with `HEKATE_PLAN_CONTRACT=1` and the dispatcher disabled. The run fails if the port is in use.

Cleanup kills the Api, drops the database and verifies the drop. Each step is independent and errors are aggregated, so an original error is never masked. The `.run/` work folder, including `api.log`, is deleted only after a confirmed Api exit and database drop. It is kept when startup or any test fails.

## Supervisor rules under test

- The supervisor dispatches only on a **fresh** claim (`replayed=false`) that is current at hand-off. A replayed receipt or an explicit `uncertain` operator state means needs-operator, whatever `stillCurrent` says.
- Success comes from typed outcome fields only, with runtime type checks: `exitCode` an int equal to 0, `timedOut` and `killed` the bool `false`, a structured result and a non-blank artifact. It also requires a strict-type match on every correlation field. Prose is never read.
- Before finishing:
  - the re-read receipt is unchanged and `stillCurrent`;
  - the node's current attempt matches;
  - the current `stateRevision` is used only for the compare-and-set.
- PlanStore is the final authority. A rejected finish becomes needs-operator, with no retry.
- Transition idempotency is last-operation only. The held finish is re-sent only while current state proves it is still the node's last operation; otherwise nothing is sent and the case goes to reconciliation.
- Release only on explicit operator instruction, with key `supervisor:<claimKey>:release`. The supervisor never calls `decide`.
- There is no launch journal in E1a, so restart durability is **not** claimed; the replay cases test policy only.
