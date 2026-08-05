# Failure Handling

Hekate uses deterministic pattern matching for common failures and LLM-based diagnosis for the rest. The failure handling system is built into Odin's dispatch module and draws from Netflix Conductor's retry patterns.

---

## Diagnosis Pipeline

When a task fails, Odin runs `diagnose_failure()`:

1. **Check retry budget**: If `retry_count >= max_retries`, skip immediately (confidence: 0.9)
2. **Pattern match**: Scan the error message against known error signatures
3. **Provider-aware fallback**: If the current provider failed, try alternatives
4. **Build why-chain**: Record the reasoning chain for audit

The diagnosis produces a `FailureDiagnosis` with:

| Field | Description |
|-------|-------------|
| `fix_type` | Strategy to apply: `retry_as_is`, `reassign_tier`, `modify_prompt`, `skip` |
| `confidence` | 0.0-1.0, how confident the diagnosis is |
| `root_cause` | Human-readable root cause |
| `why_chain` | Reasoning steps (5-Whys inspired) |
| `new_tier` | Target provider for `reassign_tier` |

---

## Error Pattern Categories

Odin recognizes these error patterns and maps them to fix strategies:

### Provider Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `rate limit` | `reassign_tier` | Provider rate-limited |
| `quota exceeded` | `reassign_tier` | Provider quota exhausted |
| `429` | `reassign_tier` | HTTP 429 rate limit |
| `503` | `retry_as_is` | Provider temporarily unavailable |
| `timeout` / `timed out` | `retry_as_is` | Execution timed out |

### Authentication Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `unauthorized` | `reassign_tier` | Authentication failure |
| `401` | `reassign_tier` | HTTP 401 unauthorized |
| `credential` | `reassign_tier` | Credential issue |

### Code Generation Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `syntax error` / `SyntaxError` | `modify_prompt` | Generated code has syntax errors |
| `IndentationError` | `modify_prompt` | Indentation error in generated code |
| `import error` / `ImportError` | `modify_prompt` | Missing import in generated code |
| `ModuleNotFoundError` | `modify_prompt` | Module not found in generated code |

### Git/Workspace Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `merge conflict` | `modify_prompt` | Git merge conflict |
| `worktree` | `retry_as_is` | Worktree setup issue |
| `branch` | `retry_as_is` | Git branch issue |

### Resource Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `CUDA out of memory` | `reassign_tier` | GPU memory exhausted |
| `OOM` | `reassign_tier` | Out of memory |
| `disk space` | `skip` | Disk space exhausted |

### CLI Tool Issues

| Pattern | Fix Type | Root Cause |
|---------|----------|------------|
| `not found on PATH` | `reassign_tier` | CLI tool not installed/accessible |
| `FileNotFoundError` | `reassign_tier` | Required file or binary missing |
| `zombie` | `retry_as_is` | Task became a zombie process |

---

## Fix Strategies

### `retry_as_is`

Re-run the task with the same provider and prompt. Used for transient issues (503, timeout, worktree glitches).

The task transitions: `failed` -> `pending` -> `dispatched` -> `running`.

### `reassign_tier`

Switch to a different provider. Odin walks the fallback chain, skipping providers that have already failed for this task.

```
Fallback chain: claude_code → ollama
```

If all providers in the chain have failed, Odin re-selects the original provider with modified approach (the "all alternatives exhausted" path).

!!! note "Budget-sensitive routing"
    When remaining budget is below $1.00, Ollama is pushed to the front of the candidate list regardless of the tier map.

### `modify_prompt`

Retry with additional guidance injected into the task prompt. The diagnosis includes `prompt_guidance` with specific instructions (e.g., "Fix the SyntaxError in the generated code -- ensure proper indentation").

### `skip`

Skip the task entirely. Used when:

- Retries are exhausted (`retry_count >= max_retries`)
- Disk space is full (no retry will help)
- The task is fundamentally unrecoverable

Skipped tasks are marked `failed` to unblock the wave. Dependent tasks may still proceed if they can tolerate missing input.

### `escalate`

Not a pattern-match outcome -- escalation happens when:

- Diagnosis confidence is below the threshold
- Multiple fix types have been tried without success
- The task is moved to `needs_review` for human intervention

---

## Retry Backoff

Retry timing follows Conductor-style `TaskDefinition` policies:

### Fixed (default)

```
Attempt 1: wait 60s
Attempt 2: wait 60s
Attempt 3: wait 60s
```

### Exponential Backoff

```
Attempt 1: wait 60s
Attempt 2: wait 120s  (60 * 2^1)
Attempt 3: wait 240s  (60 * 2^2)
```

### Linear Backoff

```
Attempt 1: wait 120s  (60 * 2.0 * 1)
Attempt 2: wait 240s  (60 * 2.0 * 2)
Attempt 3: wait 360s  (60 * 2.0 * 3)
```

All strategies are capped at 3,600 seconds (1 hour) per delay.

### Configuring retry per task type

Task definitions can be registered in the task type registry:

```python
from gods.task_definition import TaskDefinition, RetryLogic

defn = TaskDefinition(
    retry_count=5,
    retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
    retry_delay_seconds=30,
    backoff_rate=2.0,
    timeout_seconds=900,  # 15 minutes
)
```

---

## Deadlock Detection

Odin detects deadlocks when a wave cannot make progress:

1. All non-terminal tasks in the current wave are `failed`
2. All have exhausted their retry budgets
3. No tasks are `running` or `pending`

When a deadlock is detected, Odin:

- Logs the deadlock with full task details
- Emits a `deadlock_detected` event
- Escalates remaining tasks to `needs_review` or marks the project `failed`

---

## Human Escalation

Tasks reach `needs_review` through several paths:

| Trigger | Description |
|---------|-------------|
| Mimir verdict: `HUMAN_NEEDED` | Verification found issues requiring human judgment |
| Retry exhaustion | All retries failed, task escalated |
| Deadlock | Wave is stuck, remaining tasks need review |
| Code review rejection | Review cycle found security/correctness issues |

### Resolving `needs_review` tasks

**Via Prometheus MCP:**
```
Use mcp__prometheus__approve_task with task_id: "abc123"
```

**Via REST API:**
```bash
curl -X POST http://localhost:5200/api/tasks/abc123/approve \
  -H "Authorization: Bearer $TOKEN"
```

Approving moves the task to `completed`. Rejecting moves it back to `pending` for re-execution.

---

## Provider Fallback Chain

The current active fallback chain:

```
claude_code (primary) → ollama (local, free)
```

Gemini CLI and Codex CLI are disabled due to auth issues. All non-asset tasks route to `claude_code` by default.

| Provider | Status | Best For |
|----------|--------|----------|
| `claude_code` | Active | All code, research, analysis, integration, docs |
| `ollama` | Active | Asset tasks, budget-constrained fallback |
| `gemini_cli` | Disabled | Auth broken (exit code 1) |
| `codex_cli` | Disabled | ChatGPT subscription broken |

---

## Gate Failures

Before dispatch, tasks pass through gates (budget check, provider availability). If a gate fails:

1. The task stays in `pending` state
2. The gate failure reason is logged
3. On the next tick, Odin re-evaluates the gate
4. If the gate keeps failing (e.g., budget exhausted), the project eventually stalls

Gate failures do not count as task failures and do not consume retries.

!!! tip "Monitoring gate failures"
    Watch the pipeline logs for `Tyche: budget` messages or `Provider availability check failed` warnings. These indicate systemic issues that affect all tasks, not just individual failures.
