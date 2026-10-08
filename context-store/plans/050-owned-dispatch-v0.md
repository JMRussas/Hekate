# Plan 050: Owned dispatch v0 (bounded dispatcher over the persistent local store)

**Status: IMPLEMENTED, unit-tested offline, NOT yet run against a live store (048 is taken by the codex worker, 049 by task navigation).** LOCAL development only. Module: `scripts/local/supervisor_e1/e1/owned_dispatch.py`; tests: `tests/test_owned_dispatch.py`. Builds on plan 042 (plan-run), 043 (local store) and 045 (recovery stays unimplemented and unused here).

## 1. Purpose

`plan_cli run --store local` drives a plan only while that command's process lives, and nothing records that it is alive. This layer is a small loop around the same primitives that survives a chat turn, is the only owner of the store, and writes an observation file saying what it is doing and why it stopped. It adds no engine, no scheduler, no service and no database.

## 2. Authority

PlanStore remains the only authority for claims, task state and acceptance. Everything under `<state-dir>/dispatch/` is an _observation of the dispatcher process_: `status.json` (atomic replace), `events.jsonl` (append-only), `stop.request`, `dispatcher.log`. Nothing resumes from those files; a restart re-reads PlanStore and the run roots.

## 3. Mechanics

| Concern         | Rule                                                                                                                                                                                                                                                                                            | Reused primitive                                         |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Exclusive owner | the dispatcher opens `LocalStore` (PostgreSQL advisory lock keyed by the marker); a second opener, including `plan_cli run --store local`, gets `store_in_use`. A live or unresponsive status owner is also refused earlier (`owner_running` / `owner_unresponsive` / `owner_owner_unverified`) | `LocalStore.open/_acquire_lock`                          |
| Pins            | `--plan-sha256` must equal the plan file bytes; specs are the plan's hash-pinned specs; the executable and its sha256; `--root-go`; run root bound to store+plan (`plan_changed`, `binding_mismatch`, `run_root_unbound`)                                                                       | `plan_cli.check_run_inputs/check_binding`, `plan_import` |
| Observe         | one PlanStore read per cycle, classified with `plan_run.snapshot/classify/root_container`                                                                                                                                                                                                       | plan_run                                                 |
| Dispatch        | `plan_run.run_plan`, bounded by the remaining node budget, after a fresh `store.session` (which refuses uncertain operator acts)                                                                                                                                                                | plan_run, task_runner, local_store                       |
| Failure         | any dispatch that does not end accepted, or raises, is `failed`; the dispatcher exits. **Never retried.**                                                                                                                                                                                       |                                                          |
| Restart         | no resume. It reads PlanStore: `in_progress` -> `blocked/inflight` and nothing is dispatched; an existing node run root -> `node_run_root_exists`; the dead predecessor is only recorded (`previousOwner`, `wasDispatching`)                                                                    | plan_run stops                                           |

`plan_run.run_plan` gained two optional, additive parameters (default None = unchanged behaviour): `stop_requested` and `max_nodes`. They are checked only after the authoritative read picked the next node and before anything is claimed, giving the stops `stop_requested` and `node_limit`. They never interrupt a node in flight.

## 4. Public states (`status.json` `state`, plus derived liveness)

| state                    | meaning                                                                                                                                                                                                                                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `running`                | THIS dispatcher is inside `run_plan` for `current.node` (owned pid + matching native process creation identity + fresh heartbeat).                                                                                                                                                                                      |
| `blocked`                | stop reason names it: `spec_pending`, `review_pending` (the dispatcher keeps polling these two until a bound), or `inflight`, `acceptance_stale`, `node_rejected`, `node_cancelled`, `no_ready_work`, `plan_drift`, `spec_mismatch`, `base_not_chained`, `uncertain_operator_acts`, ... (the dispatcher exits, exit 1). |
| `failed`                 | a dispatch this process started did not end accepted, raised, or PlanStore was unreadable. Exit 1, no retry.                                                                                                                                                                                                            |
| `stopped`                | clean exit: `stop_requested` or `duration_elapsed` (exit 0).                                                                                                                                                                                                                                                            |
| `ready_idle`             | a ready node with a hash-valid pinned spec exists but the dispatcher did not run it: `node_limit` or `--observe-only` (exit 0).                                                                                                                                                                                         |
| `done`                   | PlanStore says every node and the root are accepted (exit 0).                                                                                                                                                                                                                                                           |
| `owner_gone` (derived)   | the file is not a clean exit and the owner pid is gone: the dispatcher died; its last node may still be `in_progress`. Inspect PlanStore; do not reset.                                                                                                                                                                 |
| `unresponsive` (derived) | pid exists but the heartbeat is stale (> 3 x interval + 5 s). Unverified.                                                                                                                                                                                                                                               |

`in_progress` is never read as "a worker is alive": it is `blocked/inflight` unless this process's own heartbeat says it is dispatching.

## 5. Defaults and hard limits

| Limit                                                                       | Default     | Allowed                |
| --------------------------------------------------------------------------- | ----------- | ---------------------- |
| `--max-duration-s`                                                          | 14400 (4 h) | 60 .. 86400            |
| `--poll-s` / `--max-poll-s` (backoff doubles while the same block persists) | 60 / 600    | 5 .. 3600, max >= poll |
| `--heartbeat-s`                                                             | 15          | 5 .. 300               |
| `--max-nodes` dispatched per dispatcher run                                 | 3           | 1 .. 20                |

The cost of a node is whatever its pinned spec allows (`budgetUsd`, `maxRounds`, `maxTurns`, step timeouts, and plan_run's 1200/600/300 s worker timeouts). The dispatcher passes no command, prompt or shell scope of its own: the only things that can run are the argv a hash-pinned spec declares and the pinned worker executable.

## 6. Status file contents

`schema owned-dispatch-status.v0`; `owner{pid, processBirth, startedAt, python, launch, storeDb, exeSha256}`; `heartbeat{seq, epoch, at, intervalS}`; `limits`; `phase` (`starting|observing|waiting|dispatching|exited`); `state`; `stopReason`; `detail` (schema-selected reason metadata; arbitrary messages/URLs omitted); `current{node, nodeId, workerLiveness:unknown, usefulProgress:unknown, since, nodeRunRoot}`; `nodes{key: work, acceptance, ready}` (a copy, labelled non-authoritative); `steps` (last 20: node, outcome, reason, evidence path); `counters`; `previousOwner`; `exitedCleanly`; `writeErrors`. Every value passes `public()`: keys that look like token/secret/password/credential/authorization/api key/prompt are replaced, strings cut to 300 chars, lists to 20, depth to 6. No prompt, transcript, environment or credential is ever written; per-attempt evidence stays in the run roots (`evidence.json`, attempt traces) and is referenced by path.

## 7. Commands (from `scripts/local/supervisor_e1`, Python 3.13.13 under uv)

```
uv run python -m e1.owned_dispatch launch --state-dir S --plan P --plan-sha256 H --run-root R --exe E --exe-sha256 X --root-go REF --launch-real-model [--worker codex --worker-model M] [limits]
uv run python -m e1.owned_dispatch status --state-dir S          # read-only; works without the store
uv run python -m e1.owned_dispatch stop   --state-dir S          # graceful
uv run python -m e1.owned_dispatch run    ...same as launch...   # foreground (what launch starts)
```

`launch` refuses a live owner and bad pins before spawning; spawns `sys.executable -m e1.owned_dispatch run ...` (argv rebuilt from parsed pins, no shell) with no console window (`CREATE_NO_WINDOW`, `SW_HIDE`), its own process group, `CREATE_BREAKAWAY_FROM_JOB` (retried without it, and reported `breakaway:false`, if the caller's job forbids it), stdin null and stdout/stderr appended to `dispatch/dispatcher.log`. It reports `launched:true` only when `status.json` names that exact child pid and carries a heartbeat (default wait 60 s; else `unconfirmed`/`launched:false`: check `status` before launching again). Exit codes: 0 done / clean stop / ready_idle, 1 blocked or failed, 2 refused before any effect.

## 8. Limits stated plainly

- Graceful stop is a file. The dispatcher finishes the node in flight (bounded by the spec) and claims no new one. A hidden process has no console, so Ctrl-C/Ctrl-Break are not a supported stop; `taskkill` of the dispatcher mid-node leaves PlanStore `in_progress` (reported `owner_gone`, then `blocked/inflight` on the next start) and is an operator incident, not something the dispatcher repairs.
- The heartbeat thread proves the dispatcher process is alive, not that the node's worker is making progress; worker-level evidence is the attempt trace in the node's run root.
- PID reuse is fenced by matching native process creation identity (Windows process FILETIME, Linux start ticks). Missing creation evidence is `owner_unverified`; a future or invalid heartbeat is `unresponsive`. This observes the dispatcher, not worker progress.
- It does not reset, reclaim, review, accept, integrate, pin specs or classify uncertain operator acts. `spec_pending`/`review_pending` clear only by an operator act (`plan_import.pin_spec` after preparing and freezing the spec; an independent review decision).
- A dispatcher started on a plan whose run root is unbound imports it and binds the root (as `plan_cli`); thereafter it only attaches.
- Single plan, single node at a time, single machine. No service, no Windows deployment, no remote or production use.
- Verified so far only by offline unit tests with injected fakes (no database, no Api, no model). A live smoke with the fake CLI on an owned store is still required before relying on it.

## Review corrections

The first candidate was rejected before live use. Six loop test fixtures lacked a valid spec loader seam; the repaired seam preserves real default validation. Independent tests fence PID reuse, future/nonfinite heartbeat, unavailable creation evidence, arbitrary reason text, and plan hash before child spawn. Public owner liveness is distinct from unknown worker liveness/useful progress. Spec-pending remains an explicit operator preparation gate. Duration and stop checks occur between nodes; an in-flight native runner retains its own finite subprocess bounds and may outlast the dispatcher duration.

Follow-up source review clarifications: `--observe-only` prevents dispatch but a first run still imports/verifies the declared plan and binds the new run directory. The real rehearsal performed an idempotent import of the existing plan and made no claim/model call. Owner setup failure always stops the acquired store. Status includes blocked task IDs independently of restricted reason detail. Launch acknowledgement verifies native creation identity; non-positive PIDs are refused. Duration uses a monotonic clock and is checked between nodes. Node/duration budgets apply per explicitly authorized launch; restarting does not imply permission to retry an uncertain node.
