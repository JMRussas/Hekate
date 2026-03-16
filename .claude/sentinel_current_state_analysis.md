# Sentinel Current State Analysis

This document details the decision points and event flow of the current orchestration architecture, focusing on `executor.py` and `task_lifecycle.py`.

## 1. Current State Analysis: Decision Points

The current system is overwhelmingly **mechanical** and rule-based. True "intelligent" decisions are confined within LLM agents (e.g., the verifier or code reviewer models). The orchestration code reacts to the output of these agents in a deterministic way.

### `backend/services/executor.py`

The executor's primary role is to discover and dispatch ready tasks based on a set of rigid rules.

#### Mechanical Decisions (Rule-Based)

*   **Main Loop (`_tick`):** The core of the executor is a continuous loop that wakes up periodically to scan for work.
*   **Project Discovery:** Identifies all projects in an `EXECUTING` state.
*   **Budget Check:**
    *   Checks if the global budget has been exhausted.
    *   Identifies if a project has only "free" tasks (CLI-based) remaining to allow it to continue even if the budget is low.
    *   Pauses projects that have no budget for their pending paid tasks.
*   **Dependency Resolution (`_update_blocked_tasks`):** Scans for `BLOCKED` tasks and unblocks them if all their dependencies have reached a terminal state (`COMPLETED` or `NEEDS_REVIEW` with output).
*   **Wave Management:**
    *   Identifies the current, lowest-numbered wave with incomplete tasks.
    *   Restricts task dispatch to only tasks within that current wave.
*   **Task Discovery:** Finds `PENDING` tasks within the current wave that have no unresolved dependencies.
*   **Dispatch Gating (per task):**
    *   **Execution Mode:** Skips tasks if the project's `execution_mode` is `EXTERNAL` or if the task's model tier doesn't match the `HYBRID` mode's responsibility.
    *   **Retry Backoff:** Skips tasks that are in a temporary cool-down period after a transient failure.
    *   **Resource Availability (`_resources_available`):** Checks if required external resources (Ollama, ComfyUI, API keys) are online using a circuit-breaker pattern.
    *   **Budget Reservation:** Reserves estimated cost for a task before dispatch; skips if insufficient budget.
    *   **Claiming:** Uses an atomic database update to claim a task, preventing multiple executors from dispatching the same task.
*   **Project Lifecycle Management:**
    *   **Wave Completion:** Detects when all tasks in a wave are finished.
        *   **PR Creation (`_create_wave_pr`):** If enabled, creates a pull request for the completed wave's changes.
        *   **Checkpoint Pause:** If enabled, pauses the project after a wave is complete, requiring user intervention to continue.
    *   **Project Completion:** Detects when all tasks in a project are in a terminal state. Sets project status to `COMPLETED` or `FAILED` based on the outcome of the tasks.
    *   **Hollow Completion Block:** A key rule that prevents a project from being marked `COMPLETED` if it contains `NEEDS_REVIEW` tasks that have no output.
    *   **Deadlock Detection:** Fails a project if it has `BLOCKED` tasks but no `PENDING`, `QUEUED`, or `RUNNING` tasks, as no forward progress is possible.
*   **State Recovery (`_recover_stale_tasks`, `_sweep_stale_tasks`):** On startup and during ticks, finds tasks that were stuck in `RUNNING` or `QUEUED` state (e.g., due to a crash) and resets them to `PENDING` or `BLOCKED`.

#### Intelligent Decisions

*   None. The executor is a pure state machine that follows predefined rules. It does not choose between multiple valid options or strategies.

### `backend/services/task_lifecycle.py`

This file contains the logic for what happens to a single task *after* it has been dispatched by the executor.

#### Mechanical Decisions (Rule-Based)

*   **Agent Dispatch:** Selects the correct Python function to run the task (e.g., `run_ollama_task`, `run_claude_task`) based on its `model_tier`.
*   **Error Handling:**
    *   Catches transient network/API errors (`_TRANSIENT_ERRORS`).
    *   If `retry_count` < `max_retries`, it resets the task to `PENDING` and schedules a retry with exponential backoff.
    *   If retries are exhausted, it either fails the task or creates a user-facing checkpoint, based on the `CHECKPOINT_ON_RETRY_EXHAUSTED` config flag.
*   **Context Forwarding (`forward_context`):** Upon successful task completion, it takes the output, summarizes it, and injects it into the context of all dependent tasks.
*   **Configuration Checks:** Most features are gated by `if X_ENABLED:` checks (e.g., `VERIFICATION_ENABLED`, `REVIEW_CYCLE_ENABLED`).
*   **Identical Output Check:** During verification retries, if a task produces the exact same output that was just rejected, it is escalated to `NEEDS_REVIEW` to break a potential loop.
*   **Iteration Limits:** The code review cycle is hard-capped by `REVIEW_MAX_ITERATIONS` to prevent infinite loops, escalating to `NEEDS_REVIEW` if the limit is reached.

#### Intelligent Decisions (Orchestrating Intelligence)

While the lifecycle code itself is rule-based, it is responsible for invoking and reacting to other intelligent agents.

*   **Verification (`verify_task_output`):**
    *   **Decision:** An LLM-based verifier analyzes the task's output. It decides if the output is `VERIFIED`, has `GAPS_FOUND`, or `HUMAN_NEEDED`.
    *   **Reaction (Mechanical):**
        *   If `GAPS_FOUND`, the task is reset to `PENDING` for a retry, and the verifier's feedback is added to the task's context.
        *   If `HUMAN_NEEDED`, the task status is set to `NEEDS_REVIEW`.
*   **Code Review (`_run_review_cycle`):**
    *   **Decision:** An LLM-based agent reviews the git diff produced by a coding task. It decides if the code is `approved` or has issues.
    *   **Reaction (Mechanical):**
        *   If `approved`, the code is auto-committed (if enabled) and the task proceeds to completion.
        *   If not `approved`, the task is reset to `PENDING` for another iteration, and the reviewer's feedback is added to the context.

## 2. Current Event Flow

The current flow is a linear, database-driven polling process.

```mermaid
graph TD
    subgraph User Action
        A[API Request via Extension/CLI] --> B{Update Project Status to EXECUTING};
    end

    subgraph "Executor Tick Loop (executor.py)"
        C[Loop Start] --> D{Find EXECUTING Projects};
        D --> E{Check Budget & Branch};
        E --> F{Unblock Tasks with Met Dependencies};
        F --> G{Determine Current Wave};
        G --> H{Find Ready PENDING Tasks in Wave};
        H --> I{For each Ready Task...};
        I -- Task Ready --> J[Gate: Check Resources, Backoff, etc.];
        J -- Pass --> K[Claim Task: Update Status to QUEUED];
        K --> L[Dispatch: asyncio.create_task(execute_task)];
        I -- No more tasks --> M{Check for Wave/Project Completion};
        M --> N[Update Project Status / Create PR];
        N --> O[Sleep for TICK_INTERVAL];
        O --> C;
    end

    subgraph "Single Task Lifecycle (task_lifecycle.py)"
        L --> P[Acquire Semaphore];
        P --> Q[Update Status to RUNNING];
        Q --> R[Run Agent (e.g., run_claude_task)];
        R -- Success --> S{Verification (Optional)};
        S -- Verified --> T{Code Review (Optional)};
        T -- Approved --> U[Update Status to COMPLETED];
        U --> V[Forward Context to Dependents];
        V --> W[Release Semaphore];
        R -- Transient Error --> X{Retries Left?};
        X -- Yes --> Y[Reset to PENDING w/ Backoff];
        Y --> W;
        X -- No --> Z[Set to FAILED or NEEDS_REVIEW];
        Z --> W;
        S -- Gaps Found --> X;
        T -- Changes Requested --> X;
    end
    
    subgraph "Progress Events"
        B -- project_executing --> AA(SSE Stream);
        Q -- task_start --> AA;
        Y -- task_retry --> AA;
        Z -- task_failed --> AA;
        U -- task_complete --> AA;
        N -- project_complete/failed --> AA;
    end
```
