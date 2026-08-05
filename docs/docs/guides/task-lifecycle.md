# Task Lifecycle

Every task in Hekate follows a formal state machine. Understanding the states and transitions is essential for debugging stuck projects, writing custom handlers, and configuring retry behavior.

---

## State Machine

```
                ┌──────────┐
                │  pending  │◄──────────────────────────────┐
                └────┬──┬──┘                                │
                     │  │                                   │
            ┌────────┘  └────────┐                          │
            ▼                    ▼                          │
     ┌──────────┐        ┌────────────┐                    │
     │ blocked  │        │ dispatched │                    │
     └────┬─────┘        └─────┬──────┘                    │
          │                    │                            │
          │                    ▼                            │
          │              ┌──────────┐                       │
          │              │ running  │                       │
          │              └────┬──┬──┘                       │
          │                   │  │                          │
          │          ┌────────┘  └────────┐                │
          │          ▼                    ▼                │
          │   ┌───────────┐        ┌──────────┐           │
          │   │ completed │        │  failed  │───────────┘
          │   └─────┬─────┘        └────┬─────┘     (retry)
          │         │                   │
          │         ▼                   ▼
          │   ┌──────────────┐   ┌──────────────┐
          │   │ needs_review │   │  cancelled   │
          │   └──────────────┘   └──────────────┘
          │
          └──► cancelled
```

---

## States

| State | Description |
|-------|-------------|
| `pending` | Ready to be dispatched. All dependencies satisfied. |
| `blocked` | Waiting for upstream dependencies to complete. |
| `queued` | Accepted by Hermes but waiting for a concurrency slot. |
| `dispatched` | Odin has assigned a provider. Hermes will pick it up. |
| `running` | Hermes has launched a CLI subprocess. Heartbeats active. |
| `completed` | Execution finished. Output verified by Mimir. |
| `failed` | Execution or verification failed. May be retried. |
| `cancelled` | Manually or automatically cancelled. Terminal. |
| `needs_review` | Requires human review before proceeding. |

---

## Valid Transitions

| From | To | Trigger |
|------|----|---------|
| `pending` | `dispatched` | Odin selects a provider and emits `dispatch_command` |
| `pending` | `blocked` | Task has unsatisfied dependencies |
| `pending` | `running` | Direct execution (skip dispatch) |
| `pending` | `cancelled` | Project cancellation or manual cancel |
| `blocked` | `pending` | All dependencies completed; task unblocked |
| `blocked` | `cancelled` | Project cancellation |
| `queued` | `dispatched` | Concurrency slot opens |
| `queued` | `running` | Direct execution from queue |
| `queued` | `cancelled` | Project cancellation |
| `dispatched` | `running` | Hermes launches the subprocess |
| `dispatched` | `pending` | Dispatch failed; return to pool |
| `dispatched` | `cancelled` | Project cancellation |
| `running` | `completed` | Execution succeeded and Mimir verified output |
| `running` | `failed` | Execution error, timeout, or verification failure |
| `running` | `cancelled` | Manual cancel or project cancellation |
| `completed` | `pending` | Retry requested (e.g., review found issues) |
| `completed` | `needs_review` | Mimir or code reviewer flags concerns |
| `failed` | `pending` | Odin diagnosis recommends retry |
| `failed` | `cancelled` | Retries exhausted or manual cancel |
| `failed` | `needs_review` | Escalated for human review |
| `needs_review` | `completed` | Human approves the task |
| `needs_review` | `pending` | Human requests re-execution |
| `needs_review` | `cancelled` | Human cancels the task |

!!! note "Soft enforcement"
    The state machine currently logs invalid transitions as warnings but still allows them. This is intentional -- the system ran without formal state validation for months, so edge cases may exist. Invalid transitions are logged for audit but not blocked.

---

## Dispatch (Odin)

When the pipeline ticks, Odin scans for dispatchable tasks:

1. **Find ready tasks**: Query tasks with `status = 'pending'` whose dependencies are all `completed`.
2. **Check provider availability**: Call LLM Gateway (`GET /providers`) to see which providers are online.
3. **Select provider**: Use the tier map (`_TIER_MAP`) to match task type + complexity to a provider. Walk the fallback chain if the preferred provider is unavailable.
4. **Budget gate**: Tyche checks if budget allows the dispatch.
5. **Emit `dispatch_command`**: Includes task ID, project ID, provider, timeout, and reason.

### Provider Selection

The tier map routes tasks to providers based on type and complexity:

| Task Type | Complexity | Default Provider |
|-----------|-----------|-----------------|
| `code` | any | `claude_code` |
| `research` | any | `claude_code` |
| `analysis` | any | `claude_code` |
| `integration` | any | `claude_code` |
| `documentation` | any | `claude_code` |
| `asset` | simple/medium | `ollama` |

Fallback chain: `claude_code` -> `ollama`. If budget is tight (< $1.00 remaining), Ollama is pushed to the front of the candidate list.

---

## Execution (Hermes)

Hermes is the async execution engine. It manages CLI subprocesses without blocking the pipeline.

### Flow

1. **Receive `dispatch_command`**: Hermes checks for available concurrency slots (default: 4 max concurrent).
2. **Queue if full**: If all slots are occupied, the task is deferred and drained when a slot opens.
3. **Launch subprocess**: Starts a CLI process (e.g., `claude -p "..."`) with the task prompt, repo path, and MCP configuration.
4. **Monitor**: A background coroutine awaits the subprocess, collecting stdout/stderr.
5. **Heartbeats**: Every 30 seconds, Hermes writes a heartbeat to the database. If a task has no heartbeat for `response_timeout_seconds`, it is considered stale.
6. **Write results**: On completion, writes the result to the relay table for Mimir to pick up.

### Concurrency

```python
HermesRunner(
    db=db,
    max_concurrent=4,        # Max simultaneous CLI processes
    default_timeout=600,      # 10 minutes per task
    heartbeat_interval=30.0,  # Heartbeat every 30s
)
```

The `_dispatch_lock` (asyncio.Lock) serializes the concurrency check to prevent two concurrent dispatch events from both seeing available slots.

---

## Verification (Mimir)

After a task completes, Mimir verifies the output in two stages:

### Stage 1: Heuristic Check (no LLM)

Fast pattern matching on the output:

- **Empty output**: Fails immediately
- **Very short output** (< 10 characters): Fails as suspicious
- **Error-dominated output** (> 50% error lines): Fails
- **Single error line**: Fails

### Stage 2: LLM Verification

If the heuristic check passes, Mimir sends the output to the LLM Gateway for deeper review. The LLM checks:

- Whether the output addresses the task requirements
- Whether there are obvious bugs or incomplete work
- Whether tests pass (if TDD is enabled)

Possible outcomes:

| Verdict | Result |
|---------|--------|
| `PASSED` | Task marked completed. Hephaestus stages files. |
| `GAPS_FOUND` | Task marked failed with feedback. Odin may retry. |
| `HUMAN_NEEDED` | Task marked `needs_review` for human intervention. |

---

## Context Forwarding

When a task completes, its output is forwarded to dependent tasks via `context_json`. This gives downstream tasks the context they need without re-reading the codebase.

```
Task A (completed) ──output──► Task B (context_json includes A's output)
                                Task C (context_json includes A's output)
```

The forwarded context includes:
- Task title and description
- Execution output (truncated if very large)
- Affected files
- Any extracted knowledge

---

## Retry Mechanics

Retry behavior is configured per-task via `TaskDefinition` (Conductor-style):

| Setting | Default | Description |
|---------|---------|-------------|
| `retry_count` | 3 | Maximum number of retry attempts |
| `retry_logic` | `FIXED` | Backoff strategy: `FIXED`, `EXPONENTIAL_BACKOFF`, `LINEAR_BACKOFF` |
| `retry_delay_seconds` | 60 | Base delay between retries |
| `backoff_rate` | 2.0 | Multiplier for exponential/linear backoff |
| `timeout_policy` | `RETRY` | What to do on timeout: `RETRY`, `TIME_OUT_WF`, `ALERT_ONLY` |
| `timeout_seconds` | 600 | Max execution time |
| `response_timeout_seconds` | 600 | Max time without a heartbeat before rescheduling |

### Backoff calculation

```
FIXED:               delay = retry_delay_seconds
EXPONENTIAL_BACKOFF: delay = retry_delay_seconds * (2 ^ attempt)
LINEAR_BACKOFF:      delay = retry_delay_seconds * backoff_rate * attempt
```

All delays are capped at 3,600 seconds (1 hour).

### Retry flow

1. Task fails (execution error, timeout, or verification failure)
2. Odin's `diagnose_failure()` analyzes the error
3. If `fix_type` is `retry_as_is` or `retry_with_fix`, task returns to `pending`
4. Odin waits for the computed retry delay
5. Task is re-dispatched (possibly to a different provider)

!!! warning "Retry exhaustion"
    When `retry_count` reaches `max_retries`, the diagnosis returns `fix_type: "skip"` and the task is marked `failed` or escalated to `needs_review`. This prevents infinite retry loops.

---

## Wave Progression

Tasks are organized into waves based on dependency depth. Wave 0 tasks have no dependencies. Wave 1 tasks depend on wave 0, and so on.

```
Wave 0: [Task A, Task B]  ──all complete──►  Wave 1: [Task C, Task D]
                                                       ──all complete──►  Wave 2: [Task E]
```

Odin checks wave completion after each `task_verified` event:

1. Are all tasks in the current wave completed?
2. If yes, unblock tasks in the next wave (set `blocked` -> `pending`)
3. If all waves are done, mark the project `completed`

### Deadlock Detection

If a wave has tasks that are all `failed` and retries are exhausted, the pipeline is deadlocked. Odin detects this and either:

- Escalates remaining tasks to `needs_review`
- Marks the project as `failed`
