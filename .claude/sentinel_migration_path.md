# Sentinel-as-Orchestrator: Migration Path

A phased strategy to transition orchestration logic from the executor tick loop to the Sentinel, maintaining backward compatibility and zero downtime between phases.

---

## Migration Principles

1. **Additive before subtractive** — build the new path, prove it works, then remove the old
2. **Feature flags everywhere** — every phase is gated by a config toggle so we can revert instantly
3. **One decision at a time** — move one decision category per phase, validate, then move the next
4. **DB is source of truth** — both old and new code paths read/write the same DB state; they can coexist
5. **Tests before migration** — write integration tests for each decision *before* moving it

---

## Component Mapping: Current → Target

### Existing Sentinel Components → New Role

| Current Component | Current Role | Target Role | Changes Needed |
|-------------------|-------------|-------------|----------------|
| **Bus** (`sentinel/bus.py`) | Inter-sentinel pub/sub | **Command bus** — carries both observations AND dispatch commands | Add command topics, typed message validation |
| **PlanSentinel** (`sentinel/plan_sentinel.py`) | Passive observer, rule evaluator | **ProjectSentinel** — the brain, owns OODA loop | Major refactor: add world model, decision engine, command dispatch |
| **SystemSentinel** (`sentinel/system_sentinel.py`) | Fleet health monitor | **Fleet controller** — spawns ProjectSentinels, enforces global limits | Add worker pool allocation, global concurrency caps |
| **Reasoner** (`sentinel/reasoner.py`) | Advisory diagnosis | **Decision engine core** — consulted for all intelligent decisions | Expand prompt, add confidence calibration, decision history context |
| **InterventionExecutor** (`sentinel/intervention_executor.py`) | REST-based nudges | **Command dispatcher** — in-process async commands to worker pool | Replace REST calls with direct async method calls |
| **ContextClient** (`sentinel/context_client.py`) | Observation persistence | **Decision store** — persist decisions + observations | Add `DecisionRecord` persistence, semantic search for decisions |
| **Models** (`sentinel/models.py`) | Observations, health | **World model + commands** — full project state representation | Add `ProjectWorldModel`, `SentinelCommand`, `ExecutionStrategy` |

### Executor Functions → Where They Go

| Executor Function | Target Owner | Migration Phase |
|-------------------|-------------|-----------------|
| `_tick()` main loop | ProjectSentinel decision loop | Phase 4 (final) |
| `_update_blocked_tasks()` | ProjectSentinel rules (`check_tasks_ready`) | Phase 2 |
| Wave detection (min incomplete wave) | ProjectSentinel rules (`check_wave_complete`) | Phase 2 |
| Dispatch gating (mode/backoff/resources) | ProjectSentinel orient phase | Phase 3 |
| `_resources_available()` | WorkerPool `ResourceMonitor` → reports to Sentinel | Phase 1 |
| Budget reservation | ProjectSentinel act phase | Phase 3 |
| Atomic task claiming (CAS) | WorkerPool `dispatch()` | Phase 3 |
| Wave completion detection | ProjectSentinel rules | Phase 2 |
| `_create_wave_pr()` | ProjectSentinel command (ADVANCE_WAVE) | Phase 2 |
| Project completion/failure | ProjectSentinel rules + decide | Phase 3 |
| Dead project detection | ProjectSentinel rules (`check_dead_project`) | Phase 2 |
| `_recover_stale_tasks()` | ProjectSentinel startup recovery | Phase 4 |
| `_sweep_stale_tasks()` | ProjectSentinel rules (`check_task_stuck` — already exists) | Phase 1 |

### Task Lifecycle Functions → Where They Go

| Lifecycle Function | Target Owner | Migration Phase |
|-------------------|-------------|-----------------|
| `execute_task()` main flow | WorkerPool worker (stripped of decisions) | Phase 3 |
| Agent dispatch routing | WorkerPool (unchanged, pure mechanical) | Stays |
| Context enrichment | WorkerPool (pre-dispatch, sentinel provides context) | Phase 3 |
| Verification (`verify_task_output`) | Sentinel-commanded post-processing | Phase 5 |
| Code review (`_run_review_cycle`) | Sentinel-commanded post-processing | Phase 5 |
| Context forwarding (`forward_context`) | ProjectSentinel on task_completed | Phase 3 |
| Error handling / retry scheduling | ProjectSentinel decide phase | Phase 3 |
| Knowledge extraction | ProjectSentinel post-completion hook | Phase 5 |
| Telemetry push | ProjectSentinel post-completion hook | Phase 5 |

---

## Phase 0: Foundation (Safe, No Behavior Change)

**Goal**: Build the infrastructure the new architecture needs without touching any existing logic.

**Duration**: ~1 week

### Steps

1. **Add `ProjectWorldModel` dataclass** to `sentinel/models.py`
   - Tasks, waves, budget, resource health, timing, decision log
   - Pure data structure, no behavior yet
   - Test: unit tests for serialization/deserialization

2. **Add `SentinelCommand` and `CommandType` enum** to `sentinel/models.py`
   - All command types from target architecture doc
   - `DecisionRecord` dataclass for audit trail
   - `ExecutionStrategy` dataclass (replaces scattered config flags)

3. **Add `sentinel_decisions` table** to SQLite schema
   - Mirrors `DecisionRecord` fields
   - Index on `project_id`, `timestamp`
   - Migration script, backward compatible (additive only)

4. **Add command topics to Bus**
   - New valid topics: `dispatch_command`, `worker_event`, `decision_made`
   - Existing topics unchanged
   - Test: bus routing tests for new topics

5. **Create `WorkerPool` class skeleton** in `sentinel/worker_pool.py`
   - `dispatch()`, `cancel()`, `events()`, `available_slots`
   - Initially just wraps existing `execute_task()` from task_lifecycle
   - No behavior change — just an abstraction layer

6. **Create `SentinelRules` class** in `sentinel/rules.py`
   - Extract detection rules from PlanSentinel into standalone, stateless functions
   - `check_task_stuck()`, `check_wave_stalled()`, `check_cascade_failure()`, `check_budget_warning()`
   - PlanSentinel calls these instead of inline logic (refactor, not rewrite)
   - Test: existing sentinel tests still pass, new unit tests for extracted rules

### Validation

- All existing tests pass
- No behavior changes observable by user
- New classes are importable and unit-tested
- `sentinel_decisions` table exists but is empty

---

## Phase 1: Sentinel Gains Eyes (Parallel Operation)

**Goal**: The ProjectSentinel builds and maintains a `ProjectWorldModel` from worker events, but does NOT act on it yet. Runs alongside the existing executor.

**Duration**: ~1 week

**Feature flag**: `SENTINEL_WORLD_MODEL_ENABLED` (default: false)

### Steps

1. **ProjectSentinel builds world model from SSE events**
   - On `task_start` → update `tasks[id]`, `dispatched[id]`, timing
   - On `task_complete` → update `completed`, remove from `dispatched`
   - On `task_failed` → update `failed[id]` with error details
   - On `budget_warning` → update `budget_spent`
   - On `wave_checkpoint` → update `current_wave`
   - On `project_complete`/`project_failed` → terminal state
   - Periodically snapshot to DB (every 30s) for crash recovery

2. **ResourceMonitor extracted from executor**
   - Move `_resources_available()` logic into `sentinel/resource_monitor.py`
   - Both executor and sentinel can call it
   - Executor still uses it directly (no behavior change)
   - Sentinel updates `world_model.resource_health` from it

3. **Zombie detection delegated to sentinel**
   - Sentinel's existing `check_task_stuck()` already detects stuck tasks
   - Add: when sentinel detects stuck task, it publishes to bus AND calls existing `release_claim` intervention
   - Executor's `_sweep_stale_tasks()` remains as safety net but logs when sentinel already handled it
   - Reduces duplicate detection, proves sentinel can act

4. **Decision logging starts**
   - Every intervention the sentinel takes is now recorded as a `DecisionRecord`
   - Persist to `sentinel_decisions` table
   - No new decisions yet — just recording existing ones

### Validation

- World model accurately reflects DB state (write comparison test)
- ResourceMonitor produces same results whether called from executor or sentinel
- Zombie tasks are handled by sentinel before executor sweep catches them
- `sentinel_decisions` table has records for every intervention

```
Executor (unchanged)  ──────────────────→  Tasks
     │                                       │
     │  SSE events                          │
     ▼                                       ▼
Sentinel (observing)                    Progress DB
     │
     ▼
World Model (shadow)
     │
     ▼
Decision Log (recording only)
```

---

## Phase 2: Sentinel Gains Voice (State Detection)

**Goal**: Sentinel takes over state detection — wave completion, project completion, dead project detection. Executor still dispatches tasks.

**Duration**: ~1-2 weeks

**Feature flag**: `SENTINEL_STATE_DETECTION_ENABLED` (default: false)

### Steps

1. **Add dispatch-oriented rules to `SentinelRules`**
   ```
   check_tasks_ready(model) → list[TaskReady]
   check_wave_complete(model) → bool
   check_project_complete(model) → ProjectTermination | None
   check_dead_project(model) → bool
   check_hollow_completions(model) → list[str]
   ```
   - These are direct ports of executor logic, operating on the world model instead of DB queries
   - Unit tested against same scenarios as executor integration tests

2. **Sentinel publishes state observations to bus**
   - `wave_complete` → bus topic `state_change`
   - `project_complete` → bus topic `state_change`
   - `dead_project` → bus topic `state_change`
   - `tasks_ready` → bus topic `dispatch_advisory` (informational, not acted on yet)

3. **Executor subscribes to sentinel state observations**
   - When sentinel says `wave_complete`, executor skips its own wave completion check
   - When sentinel says `project_complete`, executor skips its own project completion check
   - Fallback: if sentinel is disabled or hasn't published, executor does its own check (backward compat)
   - **Key pattern**: executor checks `if sentinel_said_X else self._check_X()`

4. **Wave PR creation triggered by sentinel**
   - On `wave_complete` observation, sentinel issues `ADVANCE_WAVE` command
   - Command handler calls existing `_create_wave_pr()` (moved to shared utility)
   - Executor no longer calls `_create_wave_pr()` when sentinel is active

5. **Dependency unblocking moved to sentinel**
   - `_update_blocked_tasks()` logic ported to sentinel rules
   - Sentinel calls DB update directly (same SQL, different caller)
   - Executor skips `_update_blocked_tasks()` when sentinel is active

### Backward Compatibility Strategy

```python
# In executor._tick():
if self._sentinel_active(project_id):
    # Sentinel handles state detection — skip executor's version
    ready_tasks = self._get_sentinel_ready_tasks(project_id)
    wave_done = self._sentinel_says_wave_complete(project_id)
    project_done = self._sentinel_says_project_complete(project_id)
else:
    # Legacy path — executor does everything
    ready_tasks = self._find_ready_tasks(project_id)
    wave_done = self._check_wave_completion(project_id)
    project_done = self._check_project_completion(project_id)
```

### Validation

- With flag on: sentinel detects state changes, executor respects them
- With flag off: executor works exactly as before
- Toggle mid-execution: no corruption (both read same DB)
- Comparison test: run same plan with flag on and off, same outcome

```
Executor (dispatch only)  ──→  Tasks
     ▲                           │
     │ ready_tasks,              │ SSE events
     │ state_changes             ▼
     │                      Progress DB
Sentinel (detecting)            │
     │                          │
     ▼                          │
World Model ◄───────────────────┘
     │
     ▼
Rules Engine → State Observations → Bus
```

---

## Phase 3: Sentinel Gains Hands (Dispatch Control)

**Goal**: Sentinel decides what to dispatch and when. Executor becomes a worker pool that receives commands.

**Duration**: ~2 weeks

**Feature flag**: `SENTINEL_DISPATCH_ENABLED` (default: false)

### Steps

1. **WorkerPool wraps execute_task()**
   - `WorkerPool.dispatch(command)` → acquires semaphore, spawns `execute_task()`
   - `execute_task()` simplified: no retry logic, no verification, no review cycle
   - On completion: publishes `WorkerEvent` to sentinel's event queue
   - On failure: publishes `WorkerEvent` with error details (does NOT retry)

2. **Sentinel issues DISPATCH_TASK commands**
   - Orient phase: `check_tasks_ready()` identifies candidates
   - Decide phase: for each candidate, apply gating:
     - Execution mode check (MECHANICAL — direct port)
     - Retry backoff check (MECHANICAL — direct port)
     - Resource availability (MECHANICAL — via ResourceMonitor)
     - Budget reservation (MECHANICAL — direct port)
   - Act phase: issue `DispatchCommand` to WorkerPool

3. **Retry logic moves to sentinel**
   - Worker reports `task_failed` → sentinel receives event
   - Sentinel evaluates: transient error? context issue? wrong tier?
   - **Mechanical path** (retry_count < max, known transient): auto-retry with backoff
   - **Intelligent path** (ambiguous failure, repeated errors): consult reasoner
   - Sentinel issues RETRY_TASK or REQUEUE_WITH_CONTEXT or REASSIGN_TIER command

4. **Budget control moves to sentinel**
   - Sentinel tracks `budget_spent` and `budget_reservations` in world model
   - On dispatch: sentinel reserves budget (same calculation, different location)
   - On completion/failure: sentinel releases reservation
   - Budget exhaustion: sentinel decides — pause? downgrade tiers? complete with free tier?
   - **This is the first truly intelligent decision** (previously binary pause/continue)

5. **Context forwarding moves to sentinel**
   - On `task_completed` event, sentinel calls `forward_context()` before dispatching dependents
   - Same logic, different trigger point (event-driven vs inline)

6. **Executor tick loop becomes thin**
   ```python
   async def _tick(self):
       """Legacy tick — only active when sentinel dispatch is disabled."""
       if self._sentinel_dispatch_active():
           # Nothing to do — sentinel is dispatching
           # Just sweep for zombie executor tasks (safety net)
           await self._sweep_stale_tasks()
           return
       # ... existing tick logic unchanged ...
   ```

### Critical Migration: The Tick Loop

This is the most delicate step. The executor tick loop is the heartbeat of the system. The sentinel's OODA loop replaces it, but they must not both dispatch simultaneously.

**Mutual exclusion strategy**:
- Per-project flag in DB: `orchestrator = 'executor' | 'sentinel'`
- Set on project execution start based on `SENTINEL_DISPATCH_ENABLED`
- Executor checks this flag at top of per-project loop, skips if `'sentinel'`
- Sentinel checks this flag before entering decision loop, skips if `'executor'`
- No project can have both active simultaneously

**Handoff for in-progress projects**:
- When enabling sentinel dispatch, existing EXECUTING projects stay with executor
- Only new executions use sentinel
- On executor restart with sentinel enabled: existing projects migrate to sentinel

### Validation

- With flag on: sentinel dispatches all tasks, executor idle
- With flag off: executor dispatches all tasks, sentinel observes only
- Same plan, same outcome, either path
- Budget tracking accurate to the cent
- Retry behavior matches existing for known transient errors
- No double-dispatch (mutex verified by integration test)

```
Sentinel (brain)
     │
     │ DISPATCH_TASK
     ▼
WorkerPool
     │
     │ execute_task()
     ▼
Worker → task_completed/task_failed
     │
     │ WorkerEvent
     ▼
Sentinel → decide next action
```

---

## Phase 4: Executor Becomes Worker Pool (Identity Change)

**Goal**: Remove the executor's decision-making code. It is now purely a worker pool.

**Duration**: ~1 week

**Feature flag**: `LEGACY_EXECUTOR_ENABLED` (default: false — inverted! legacy is now opt-in)

### Steps

1. **Rename and restructure**
   - `executor.py` → `worker_pool.py` (or keep `executor.py` as thin wrapper)
   - Remove `_tick()`, `_update_blocked_tasks()`, `_check_wave_completion()`, `_check_project_completion()`
   - Remove `_sweep_stale_tasks()` (sentinel handles this)
   - Remove retry scheduling (`_retry_after` dict)
   - Remove budget reservation logic
   - Keep: semaphore management, task execution dispatch, resource probing

2. **Simplify execute_task()**
   - Remove: retry catch-and-reschedule blocks
   - Remove: inline verification and review cycle
   - Remove: context forwarding (sentinel does this)
   - Keep: agent dispatch routing, output capture, error reporting
   - Result: `execute_task()` is ~200 lines instead of ~400

3. **SystemSentinel takes over project lifecycle**
   - `POST /api/projects/{id}/execute` → SystemSentinel spawns ProjectSentinel
   - ProjectSentinel initializes world model, enters OODA loop
   - No more "executor tick discovers EXECUTING projects"

4. **Legacy executor preserved behind flag**
   - `LEGACY_EXECUTOR_ENABLED=true` restores old behavior
   - Implemented as: import old executor module, run its tick loop
   - Safety net for rollback

5. **Route migration**
   - `/api/projects/{id}/execute` → sentinel endpoint
   - `/api/tasks/{id}/retry` → sentinel endpoint (sentinel decides, worker executes)
   - `/api/external/tasks/{id}/complete` → sentinel receives, runs post-processing
   - Legacy routes still work but delegate to sentinel internally

### Validation

- Full e2e test suite passes with sentinel as orchestrator
- Legacy flag enables old behavior, all tests still pass
- No orphaned code (dead code analysis)
- Load test: sentinel handles same throughput as executor

---

## Phase 5: Sentinel Gains Intelligence (New Capabilities)

**Goal**: Enable capabilities that were impossible with the executor architecture.

**Duration**: ~2-3 weeks

**No feature flag needed** — these are purely additive.

### Steps

1. **Verification as sentinel-commanded post-processing**
   - On `task_completed`: sentinel decides whether to verify (based on strategy, tier, budget)
   - Issues `VERIFY_OUTPUT` command to worker pool
   - Receives `verification_result` event
   - Decides: accept / retry with feedback / escalate to user
   - **New**: can skip verification for low-risk tasks, saving budget

2. **Review cycle as sentinel-commanded post-processing**
   - Same pattern as verification
   - Sentinel tracks review iterations in world model
   - Can adjust max iterations per task based on complexity

3. **Dynamic model tier reassignment**
   - On repeated task failure: reasoner evaluates if tier upgrade would help
   - Issues `REASSIGN_TIER` command (updates task in DB, re-dispatches)
   - On budget pressure: reasoner evaluates tier downgrade options

4. **Adaptive concurrency control**
   - Sentinel monitors: task duration, resource contention, error rates
   - Adjusts `max_parallel_tasks` in strategy dynamically
   - Example: reduce parallelism when cascade failure detected, increase when resources idle

5. **Re-planning capability**
   - On persistent failure: sentinel can issue `REPLAN_TASK` (decompose into subtasks)
   - On dependency deadlock: `SPLIT_TASK` or `MERGE_TASKS`
   - Requires plan service integration (existing plan endpoints)

6. **Debug/control interface**
   - `step_mode`: pause after each decision, wait for user continue
   - `dry_run`: log decisions without executing
   - `inject_event`: push synthetic event into world model (testing)
   - `override_decision`: force a specific action (user override)

### Validation

- Verification skip saves measurable budget on test suite
- Tier reassignment recovers from "wrong tier" failures
- Concurrency adjustment reduces cascade failures
- Step mode allows user to walk through execution decision-by-decision

---

## Phase Summary

```
Phase 0: Foundation          [~1 wk]   No behavior change. Build data structures.
    │
    ▼
Phase 1: Eyes                [~1 wk]   Sentinel observes and records. Parallel operation.
    │
    ▼
Phase 2: Voice               [~1-2 wk] Sentinel detects state. Executor listens.
    │
    ▼
Phase 3: Hands               [~2 wk]   Sentinel dispatches. Executor receives.
    │                                   ← CRITICAL PHASE: tick loop migration
    ▼
Phase 4: Identity            [~1 wk]   Executor → Worker Pool. Clean up.
    │
    ▼
Phase 5: Intelligence        [~2-3 wk] New capabilities enabled by architecture.
```

**Total estimated phases**: 5 active + 1 foundation
**Each phase is independently deployable and reversible via feature flag.**

---

## Risk Register

| Risk | Phase | Mitigation |
|------|-------|------------|
| Double-dispatch (sentinel + executor both dispatch same task) | 3 | Per-project mutex in DB; CAS on task claim unchanged |
| Sentinel crash mid-execution | 3-4 | Worker pool drains gracefully; SystemSentinel detects absence; restart recovers from DB |
| World model drift from DB | 1-4 | Periodic reconciliation (every 30s); on mismatch, reload from DB |
| Reasoner latency blocks dispatch | 3+ | Timeout on reasoner (2s); fallback to mechanical decision |
| Budget accounting mismatch during migration | 3 | Single source of truth (DB); both paths use same atomic reservation SQL |
| SSE event loss (sentinel misses event) | 1+ | In-process queue (not network SSE); missed events caught by periodic DB reconciliation |
| Legacy executor flag forgotten "on" | 4 | Log warning on startup; deprecation notice; remove after 2 stable releases |

---

## Testing Strategy Per Phase

### Phase 0
- Unit tests for all new dataclasses (serialization, defaults, validation)
- Schema migration test (upgrade + downgrade)

### Phase 1
- World model accuracy test: run plan, compare world model to DB after each event
- ResourceMonitor parity test: same inputs → same outputs from old and new code paths

### Phase 2
- State detection parity test: run same plan with executor-only and sentinel-detection, compare state transitions
- Toggle test: switch flag mid-execution, verify no corruption

### Phase 3
- Dispatch parity test: same plan, same dispatch order, same outcome
- Retry parity test: inject same transient errors, verify same retry behavior
- Budget parity test: same budget limit, same tasks skipped/downgraded
- Mutual exclusion test: verify no double-dispatch under concurrent load

### Phase 4
- Full e2e regression suite
- Load test: 10 concurrent projects, 50 tasks each
- Crash recovery test: kill sentinel mid-execution, verify clean restart

### Phase 5
- A/B comparison: same plan with and without intelligent features, measure quality/cost/time
- Verification skip test: verify low-risk tasks are correctly identified and skipped
- Tier reassignment test: inject tier-specific failures, verify automatic upgrade

---

## Rollback Procedure

Each phase can be rolled back independently:

1. **Set feature flag to disabled** — immediate, no restart needed (checked per-tick)
2. **Existing EXECUTING projects**: continue on current orchestrator until completion
3. **New projects**: use legacy executor path
4. **If DB schema was changed**: migration is additive-only (new tables/columns), so rollback doesn't require schema changes
5. **If sentinel state is corrupted**: delete `sentinel_decisions` rows for affected project, restart — executor recovers from task table state

---

## File Changes Summary

### New Files
| File | Phase | Purpose |
|------|-------|---------|
| `sentinel/worker_pool.py` | 0 | WorkerPool abstraction over execute_task |
| `sentinel/rules.py` | 0 | Extracted stateless rule evaluators |
| `sentinel/resource_monitor.py` | 1 | Extracted from executor._resources_available |
| `sentinel/decision_engine.py` | 2 | Mechanical + intelligent decision routing |
| `sentinel/command_dispatcher.py` | 3 | Executes SentinelCommands against worker pool and DB |
| `backend/db/migrations/add_sentinel_decisions.py` | 0 | Schema migration |

### Modified Files
| File | Phase | Changes |
|------|-------|---------|
| `sentinel/models.py` | 0 | Add ProjectWorldModel, SentinelCommand, ExecutionStrategy, DecisionRecord |
| `sentinel/bus.py` | 0 | Add command topics |
| `sentinel/plan_sentinel.py` | 1-3 | Evolve into ProjectSentinel (world model, OODA loop, dispatch) |
| `sentinel/system_sentinel.py` | 3-4 | Add project lifecycle ownership, worker pool allocation |
| `sentinel/reasoner.py` | 3 | Expand prompt, add decision history context |
| `sentinel/intervention_executor.py` | 3 | Replace REST calls with in-process commands |
| `sentinel/context_client.py` | 1 | Add DecisionRecord persistence |
| `backend/services/executor.py` | 2-4 | Gradually hollowed out, eventually becomes thin worker wrapper |
| `backend/services/task_lifecycle.py` | 3-4 | Stripped of decisions, becomes pure execution |
| `backend/routes/*.py` | 4 | Route to sentinel endpoints |
| `backend/config.py` | 0 | Add feature flags |
