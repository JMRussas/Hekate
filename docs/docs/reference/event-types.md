# Event Types Reference

All events flow through the `god_relay_events` table. Handlers subscribe to event types via `pipeline.register()`. Each event has a type, source god, JSON payload, severity, and optional idempotency key.

## Event Structure

```python
@dataclass
class Event:
    event_type: str           # Subscription key
    payload: dict[str, Any]   # Event-specific data
    source: str               # Emitting god/subsystem
    timestamp: float          # Unix epoch
    severity: str             # info, warning, error
    id: int                   # Relay table row ID (cursor tracking)
```

---

## Lifecycle Events

Events that drive project and wave state transitions.

| Event | Source | Subscribers | Description |
|-------|--------|-------------|-------------|
| `project_created` | API / MCP | Athena | A new project has been created and is ready for planning. |
| `project_planned` | Athena | Odin, Context Bridge | Planning is complete. Tasks and dependencies are in the database. |
| `project_started` | Odin | (informational) | Project status set to `executing`. First dispatch cycle triggered. |
| `project_tick` | Odin | Odin (dispatch) | Per-project tick. Triggers task dispatch for a single project. |
| `tick` | Scheduler / Mimir | Odin | Global tick. Odin scans all executing projects and emits per-project ticks. |
| `wave_complete` | Odin | Athena | All tasks in a wave have reached terminal state. Triggers reassessment. |
| `wave_assessed` | Athena | Odin (workflow) | Wave reassessment complete. Triggers DECISION evaluation if workflow edges exist. |
| `project_complete` | Odin | Hephaestus, Context Bridge | All tasks terminal, none failed. Project marked `completed`. |
| `project_failed` | Odin | (informational) | All tasks terminal but some failed, or deadlock detected. |

### Payload Schemas

**project_created**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | `string` | UUID of the new project |
| `requirements` | `string` | Project requirements text |
| `config_json` | `object` | Project configuration (planning mode, TDD, etc.) |

**project_planned**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | `string` | UUID |
| `plan_id` | `string` | UUID of the created plan |
| `task_count` | `int` | Number of tasks decomposed |
| `wave_count` | `int` | Number of execution waves |

**project_tick**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | `string` | UUID of the project to dispatch |
| `max_concurrent` | `int` | Optional concurrency override (default: 4) |

**wave_complete**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | `string` | UUID |
| `wave` | `int` | Wave number that completed |

**project_complete / project_failed**

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | `string` | UUID |
| `reason` | `string` | (project_failed only) Failure reason |

---

## Work Events

Events related to task execution, verification, and diagnosis.

| Event | Source | Subscribers | Description |
|-------|--------|-------------|-------------|
| `dispatch_command` | Odin | Hermes | Dispatch a task to a CLI provider for execution. |
| `task_running` | Hermes | (informational) | Task subprocess launched. Status set to `running`. |
| `worker_event` | Hermes | Mimir, Tyche | Task execution result (completed or failed). |
| `task_verified` | Mimir | Odin, Hephaestus, Mimir (review), Context Bridge | Task output verified by LLM. Triggers lifecycle check. |
| `task_diagnosis` | Odin | Odin (handle_diagnosis) | Failed task analyzed. Contains fix recommendation. |
| `task_rejected` | Mimir | Mimir | Verification found gaps. Task reset for retry. |
| `task_reset` | Odin / Mimir | (informational) | Task reset to pending after diagnosis or rejection. |
| `task_fork_requested` | Odin | Odin (fork handler) | Dynamic fan-out: create N sub-tasks from template. |
| `review_rejected` | (external) | Mimir | Code review rejected a task. Task reset with feedback. |
| `needs_human_review` | Odin / Mimir | (informational) | Task escalated to human. Retries exhausted or verifier uncertain. |

### Payload Schemas

**dispatch_command**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID of the task to execute |
| `project_id` | `string` | UUID of the parent project |
| `provider` | `string` | Selected CLI provider (`claude_code`, `ollama`) |

**worker_event**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `status` | `string` | `completed` or `failed` |
| `cost_usd` | `float` | Execution cost |
| `prompt_tokens` | `int` | Input tokens used |
| `completion_tokens` | `int` | Output tokens generated |
| `model_used` | `string` | Actual model identifier |
| `error` | `string` | (failed only) Error message |
| `timeout` | `bool` | (failed only) Whether failure was a timeout |
| `tdd_warning` | `string` | (optional) Warning if no test files in output |
| `affected_files` | `string[]` | Files written by Edit/Write tool calls |

**task_verified**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `confidence` | `float` | 0.0-1.0 verification confidence |
| `already_verified` | `bool` | (optional) Skipped because already verified |
| `already_done` | `bool` | (optional) Output indicated work was pre-existing |

**task_diagnosis**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `fix_type` | `string` | `retry_as_is`, `reassign_tier`, `modify_prompt`, `skip`, `escalate` |
| `confidence` | `float` | Diagnosis confidence |
| `root_cause` | `string` | Identified root cause |
| `why_chain` | `string[]` | Chain of "why" reasoning |
| `new_tier` | `string` | (reassign_tier only) Target provider |

**task_fork_requested**

| Field | Type | Description |
|-------|------|-------------|
| `source_task_id` | `string` | Task whose output drives the fork |
| `project_id` | `string` | UUID |
| `items` | `any[]` | Items to fan out over |
| `fork_group_id` | `string` | Group ID linking forked tasks |
| `template` | `object` | Task template for each fork |
| `join_task_id` | `string` | Task that waits for forked tasks |

---

## Infrastructure Events

Events related to git operations, gates, and system health.

| Event | Source | Subscribers | Description |
|-------|--------|-------------|-------------|
| `files_staged` | Hephaestus | (informational) | Git staged affected files after verification. |
| `stage_failed` | Hephaestus | (informational) | Git staging failed (syntax error or git add error). |
| `project_committed` | Hephaestus | (informational) | Project committed, pushed, and optionally PR created. |
| `commit_failed` | Hephaestus | (informational) | Git commit failed. |
| `gate_failed` | Pipeline | (informational) | A handler's gate check failed. Handler may retry. |
| `gate_passed` | Pipeline | (informational) | A handler's gate check passed (audit trail). |
| `gate_exhausted` | Pipeline | (informational) | All gate retry attempts exhausted. Handler output dropped. |
| `handler_error` | Pipeline | (informational) | A handler threw an unhandled exception. |
| `heartbeat` | Hermes | (not subscribed) | Periodic liveness signal from a running task. |

### Payload Schemas

**files_staged**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `files` | `string[]` | List of staged file paths |

**gate_failed**

| Field | Type | Description |
|-------|------|-------------|
| `handler` | `string` | Handler name that failed the gate |
| `reason` | `string` | Gate failure reason |
| `attempt` | `int` | Current attempt number |
| `max_attempts` | `int` | Total allowed attempts |
| `event_type` | `string` | Original event type |
| `original_payload` | `object` | Original event payload |
| `provider` | `string` | Provider if applicable |

**gate_exhausted**

| Field | Type | Description |
|-------|------|-------------|
| `handler` | `string` | Handler that exhausted retries |
| `event_type` | `string` | Original event type |
| `last_reason` | `string` | Last gate failure reason |
| `original_payload` | `object` | Original event payload |

---

## Informational Events

Events emitted for observability, budget tracking, and real-time streaming.

| Event | Source | Subscribers | Description |
|-------|--------|-------------|-------------|
| `budget_spent` | Tyche | (informational) | Cost recorded for a completed task. |
| `narration` | Hermes / Pipeline | (not subscribed) | Real-time streaming for peer programming mode. |
| `verification_started` | Mimir | (informational) | Background LLM verification launched. |
| `verification_deferred` | Mimir | (informational) | Verification queued because all slots are full. |
| `review_passed` | Mimir | (informational) | Code quality review completed (always advisory). |
| `task_already_running` | Hermes | (informational) | Duplicate dispatch suppressed. |
| `task_skipped` | Odin | (informational) | Task cancelled by diagnosis (skip fix). |

### Payload Schemas

**budget_spent**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `cost_usd` | `float` | Cost in USD |
| `model_used` | `string` | Model that incurred the cost |

**narration**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `type` | `string` | `text_delta`, `tool_call`, `tool_result`, `assistant` |
| `text` | `string` | (text_delta/assistant) Streamed text content |
| `tool` | `string` | (tool_call/tool_result) Tool name |
| `input` | `string` | (tool_call) Truncated tool input |
| `output` | `string` | (tool_result) Truncated tool output |

---

## Planning Events

Events for the parallel node tree planner (new Athena architecture).

| Event | Source | Subscribers | Description |
|-------|--------|-------------|-------------|
| `plan_node_created` | Athena L0 | Athena (deepen) | A plan node (epic stub) has been created. Triggers deepening. |
| `plan_node_complete` | Athena (deepen) | Athena (bubble_up) | A plan node has been fully deepened. Triggers parent completion check. |
| `plan_node_executable` | Athena (bubble_up) | Athena (materialize) | A leaf node is ready to become a task row. |

### Payload Schemas

**plan_node_created**

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | `string` | UUID of the plan node |
| `project_id` | `string` | UUID |
| `index_path` | `string` | Dotted index path (e.g., `1.2.3`) |
| `level` | `int` | Plan level (0=epic, 1=task, ...) |
| `title` | `string` | Node title |

**plan_node_complete**

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `index_path` | `string` | Dotted index path |

**plan_node_executable**

| Field | Type | Description |
|-------|------|-------------|
| `node_id` | `string` | UUID |
| `project_id` | `string` | UUID |
| `index_path` | `string` | Dotted index path |

---

## Event Deduplication

The pipeline deduplicates events during each tick to prevent floods:

| Event Type | Dedup Key | Behavior |
|------------|-----------|----------|
| `tick`, `project_tick` | `(event_type, project_id)` | Keep only the latest per project |
| `dispatch_command` | `(event_type, task_id)` | Keep only the latest per task |
| `worker_event` (status=skipped) | `(worker_skipped, task_id)` | Keep only the latest per task |
| All others | None | All instances processed |

## Idempotency Keys

Critical events use `idempotency_key` to prevent duplicate processing on replay. The key is a SHA-256 hash of deterministic parts:

```python
idem_key("hermes_complete", task_id, str(retry_count))
idem_key("project_complete", project_id, task_count)
idem_key("project_failed", project_id, "deadlock", str(blocked_count))
```

The relay table has a UNIQUE constraint on `idempotency_key`. Duplicate inserts are silently dropped.

---

## Handler Registration Map

Complete wiring from `registration.py`:

| Event Type | Handler | God | Notes |
|------------|---------|-----|-------|
| `project_created` | `athena_plan_leveled` | Athena | Legacy linear planner |
| `project_created` | `athena_l0` | Athena | Node tree planner (self-filters) |
| `plan_node_created` | `athena_deepen` | Athena | Fan-out deepening |
| `plan_node_complete` | `athena_bubble_up` | Athena | Completion propagation |
| `plan_node_executable` | `athena_materialize` | Athena | Leaf to task row |
| `wave_complete` | `athena_reassess_standalone` | Athena | Iterative replanning |
| `project_planned` | `odin_start` | Odin | Set project executing |
| `project_tick` | `odin_dispatch` | Odin | Find ready tasks, emit dispatches |
| `tick` | `odin_tick` | Odin | Scan executing projects |
| `task_verified` | `odin_lifecycle` | Odin | Wave/project completion, unblock |
| `task_diagnosis` | `odin_handle_diagnosis` | Odin | Apply fix (retry/reassign/skip/escalate) |
| `wave_assessed` | `odin_decide` | Odin | Evaluate DECISION workflow edges |
| `task_fork_requested` | `odin_fork` | Odin | Create N forked sub-tasks |
| `dispatch_command` | `hermes.handle_dispatch` | Hermes | Launch CLI subprocess |
| `worker_event` | `mimir.handle_verify` | Mimir | Launch background verification |
| `task_verified` | `mimir_review` | Mimir | Code quality review |
| `review_rejected` | `mimir_handle_review_rejection` | Mimir | Reset task with review feedback |
| `task_rejected` | `mimir_handle_task_rejection` | Mimir | Emit tick for re-dispatch |
| `task_verified` | `hephaestus_stage` | Hephaestus | Stage affected files |
| `project_complete` | `hephaestus_complete` | Hephaestus | Commit, push, PR |
| `worker_event` | `tyche_record_spend` | Tyche | Record execution cost |
| `project_planned` | `context_bridge_plan` | Context Bridge | Sync plan to context store |
| `task_verified` | `context_bridge_task_verified` | Context Bridge | Update task node in graph |
| `project_complete` | `context_bridge_project_complete` | Context Bridge | Mark plan node completed |
