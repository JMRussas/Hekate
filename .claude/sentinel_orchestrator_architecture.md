# Sentinel-as-Orchestrator: A Comprehensive Architecture Document

This document synthesizes the analysis and design for transitioning the Sentinel from a passive observer to the primary orchestration brain of the Hekate system.

---

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

---

# Sentinel-as-Orchestrator: Target Architecture

## Executive Summary

Invert the current control hierarchy. Today the Executor is the brain (tick loop, dispatch, lifecycle decisions) and the Sentinel is a passive observer that occasionally nudges via REST. In the target architecture, the **Sentinel becomes the orchestration brain** — owning project lifecycle, making all intelligent decisions, and controlling the execution strategy — while the **Executor becomes a stateless worker pool** that receives dispatch commands, runs tasks, and reports results.

---

## 1. Current vs Target: Role Inversion

```
┌─────────────────────────────────────────────────────────────┐
│                     CURRENT ARCHITECTURE                     │
│                                                              │
│  ┌──────────────────────┐     SSE events    ┌────────────┐  │
│  │      EXECUTOR         │ ───────────────→ │  SENTINEL   │  │
│  │  (brain + worker)     │                   │ (observer)  │  │
│  │                       │ ←─── REST nudges  │             │  │
│  │  • tick loop          │    (retry/release) │ • rules     │  │
│  │  • wave dispatch      │                   │ • reasoner  │  │
│  │  • budget mgmt        │                   │ • alerts    │  │
│  │  • completion logic   │                   │             │  │
│  │  • retry logic        │                   │             │  │
│  │  • resource checks    │                   │             │  │
│  │  • zombie detection   │                   │             │  │
│  └──────────────────────┘                   └────────────┘  │
│                                                              │
│  Problem: Sentinel sees problems but can't fix them.         │
│  Executor makes all decisions but has no reasoning ability.  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     TARGET ARCHITECTURE                      │
│                                                              │
│  ┌──────────────────────┐    dispatch cmds   ┌────────────┐ │
│  │      SENTINEL         │ ───────────────→  │  EXECUTOR   │ │
│  │  (orchestration brain)│                   │ (worker pool)│ │
│  │                       │ ←─── task results │             │ │
│  │  • decision loop      │                   │ • run task  │ │
│  │  • wave strategy      │                   │ • report    │ │
│  │  • budget control     │                   │ • heartbeat │ │
│  │  • retry reasoning    │                   │             │ │
│  │  • resource strategy  │                   │             │ │
│  │  • model selection    │                   │             │ │
│  │  • user interaction   │                   │             │ │
│  └──────────────────────┘                   └────────────┘  │
│                                                              │
│  Sentinel owns the "why". Executor owns the "how".           │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Component Definitions

### 2.1 Sentinel (Orchestration Brain)

The Sentinel is a **per-project autonomous controller** that owns the full project lifecycle from EXECUTING to terminal state. It operates on a continuous **observe → reason → command → observe** loop.

**Owns:**
- Project lifecycle state machine (EXECUTING → PAUSED → COMPLETED/FAILED)
- Wave progression strategy (advance, reorder, checkpoint, skip)
- Task dispatch decisions (what to run, when, on which tier)
- Budget allocation and spend strategy
- Retry policy (when to retry, with what context, or escalate)
- Model tier selection and reassignment
- Concurrency control (how many tasks in parallel)
- User interaction (proposals, approvals, pause/resume, overrides)
- All decision persistence (every choice logged with reasoning)

**Does NOT own:**
- Actually running tasks (that's the worker pool)
- Low-level tool execution (CLI invocation, API calls)
- Git operations within tasks
- File I/O within tasks

### 2.2 Executor (Worker Pool)

The Executor is a **stateless task runner** that receives dispatch commands and reports outcomes. It has no opinion about *what* to run or *when* — only *how*.

**Owns:**
- Task execution (invoke Claude, Ollama, Gemini, Codex)
- Context enrichment at dispatch time
- Output capture and structured result reporting
- Heartbeat/progress events during execution
- Graceful cancellation on command
- Resource health probing (is Ollama up? is API reachable?)

**Does NOT own:**
- Deciding which task to run next
- Retry logic or backoff strategy
- Wave progression or completion detection
- Budget decisions
- Verification or review (these become sentinel-controlled post-processing)

### 2.3 System Sentinel (Fleet Controller)

Unchanged in role but gains authority. The SystemSentinel manages cross-project concerns:
- Spawns/registers per-project Sentinels
- Detects cross-project resource contention
- Advises individual Sentinels on resource availability
- Enforces global concurrency limits

---

## 3. The Decision Loop

The core of the new architecture is the Sentinel's **OODA loop** (Observe, Orient, Decide, Act):

```
                    ┌─────────────────────┐
                    │     OBSERVE          │
                    │                      │
                    │  • Worker results    │
                    │  • Resource health   │
                    │  • Budget state      │
                    │  • Task graph state  │
                    │  • User messages     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     ORIENT           │
                    │                      │
                    │  • Rule evaluation   │
                    │  • Pattern matching  │
                    │  • Historical lookup │
                    │  • State aggregation │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     DECIDE           │
                    │                      │
                    │  • LLM reasoning     │
                    │  • Confidence check  │
                    │  • Escalation logic  │
                    │  • Strategy update   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     ACT              │
                    │                      │
                    │  • Dispatch tasks    │
                    │  • Adjust strategy   │
                    │  • Pause/resume      │
                    │  • Propose to user   │
                    │  • Persist decision  │
                    └──────────┬──────────┘
                               │
                               └──────→ (back to OBSERVE)
```

### 3.1 Observe Phase

The Sentinel maintains a **world model** — a structured, in-memory representation of the project's execution state. Every event updates this model.

```python
@dataclass
class ProjectWorldModel:
    """The Sentinel's view of the world."""
    project_id: str
    plan_id: str

    # Task graph
    tasks: dict[str, TaskState]          # task_id → current state
    task_graph: dict[str, list[str]]     # task_id → dependency_ids
    waves: dict[int, list[str]]          # wave_number → task_ids
    current_wave: int

    # Execution state
    dispatched: dict[str, DispatchInfo]  # task_id → worker assignment
    completed: set[str]
    failed: dict[str, FailureInfo]       # task_id → failure details
    blocked: set[str]

    # Resources
    resource_health: dict[str, ResourceState]  # resource → health
    worker_slots: WorkerSlotInfo               # available/total/by_tier

    # Budget
    budget_spent: float
    budget_limit: float
    budget_reservations: dict[str, float]      # task_id → reserved amount

    # Timing
    task_start_times: dict[str, datetime]
    task_last_progress: dict[str, datetime]
    wave_start_time: datetime | None
    project_start_time: datetime

    # History
    recent_observations: deque[SentinelObservation]  # last N
    decision_log: list[DecisionRecord]                # this session
```

**Event sources that update the world model:**

| Source | Events | Updates |
|--------|--------|---------|
| Worker pool | `task_started`, `task_progress`, `task_completed`, `task_failed`, `task_cancelled` | `tasks`, `dispatched`, `completed`, `failed`, timing |
| Resource monitor | `resource_online`, `resource_offline`, `resource_degraded` | `resource_health`, `worker_slots` |
| Budget tracker | `budget_reserved`, `budget_released`, `budget_warning` | `budget_spent`, `budget_reservations` |
| User | `user_approve`, `user_reject`, `user_pause`, `user_resume`, `user_override` | Various state changes |
| Self | `decision_made`, `strategy_updated` | `decision_log` |

### 3.2 Orient Phase

After each world model update, the Sentinel runs **rule evaluation** to detect actionable conditions. These are the same detection rules as today, but expanded:

```python
class SentinelRules:
    """Stateless rule evaluators. Return observations, not actions."""

    # Existing rules (from current PlanSentinel)
    def check_task_stuck(self, model: ProjectWorldModel) -> Observation | None
    def check_wave_stalled(self, model: ProjectWorldModel) -> Observation | None
    def check_cascade_failure(self, model: ProjectWorldModel) -> Observation | None
    def check_budget_warning(self, model: ProjectWorldModel) -> Observation | None

    # NEW: Dispatch rules (moved from executor tick loop)
    def check_tasks_ready(self, model: ProjectWorldModel) -> list[TaskReady]
    def check_wave_complete(self, model: ProjectWorldModel) -> bool
    def check_project_complete(self, model: ProjectWorldModel) -> ProjectTermination | None
    def check_dead_project(self, model: ProjectWorldModel) -> bool
    def check_hollow_completions(self, model: ProjectWorldModel) -> list[str]

    # NEW: Strategy rules
    def check_resource_contention(self, model: ProjectWorldModel) -> Observation | None
    def check_tier_mismatch(self, model: ProjectWorldModel) -> list[TierRecommendation]
    def check_concurrency_pressure(self, model: ProjectWorldModel) -> ConcurrencyAdvice | None
```

### 3.3 Decide Phase

The **Reasoner** is consulted for all non-trivial decisions. This is the key architectural difference — today the executor makes decisions with `if/else`; tomorrow the Sentinel reasons about them.

```python
class SentinelDecisionEngine:
    """Makes decisions based on observations and strategy."""

    async def decide(
        self,
        observation: Observation,
        world_model: ProjectWorldModel,
        strategy: ExecutionStrategy,
    ) -> Decision:
        """
        For mechanical decisions: apply rule directly.
        For intelligent decisions: consult reasoner.
        """
        if observation.decision_type == DecisionType.MECHANICAL:
            return self._apply_rule(observation, world_model)

        # Intelligent decision — consult LLM reasoner
        reasoning = await self.reasoner.analyze(
            observation=observation,
            world_model=world_model,
            strategy=strategy,
            history=world_model.decision_log[-20:],
        )

        if reasoning.confidence < strategy.auto_threshold:
            return Decision(
                action=reasoning.recommended_action,
                tier=DecisionTier.SUPERVISED,  # needs user approval
                reasoning=reasoning,
            )

        return Decision(
            action=reasoning.recommended_action,
            tier=DecisionTier.AUTO,
            reasoning=reasoning,
        )
```

**Decision classification** — which executor decisions become mechanical vs intelligent in the Sentinel:

| Current Executor Decision | Classification | Sentinel Behavior |
|--------------------------|----------------|-------------------|
| Unblock tasks with met deps | MECHANICAL | Auto: update state immediately |
| Find current wave | MECHANICAL | Auto: min incomplete wave |
| Check retry backoff elapsed | MECHANICAL | Auto: timer-based |
| Atomic task claim | MECHANICAL | Auto: dispatch to worker pool |
| Wave PR creation | MECHANICAL | Auto: trigger on wave complete |
| Budget reservation | MECHANICAL | Auto: reserve on dispatch |
| Budget exhaustion → pause | INTELLIGENT | Reasoner: pause vs. downgrade tiers vs. complete with free tier |
| Resource unavailable | INTELLIGENT | Reasoner: wait vs. reassign tier vs. skip |
| Project completion detection | INTELLIGENT | Reasoner: complete vs. retry hollows vs. partial success |
| Dead project (circular deps) | INTELLIGENT | Reasoner: fail vs. break cycle vs. re-plan |
| Retry vs. fail task | INTELLIGENT | Reasoner: retry with context vs. skip vs. reassign tier |
| Verification gaps | INTELLIGENT | Reasoner: retry vs. accept vs. escalate |
| Review iteration limit | INTELLIGENT | Reasoner: accept vs. reassign vs. human review |
| Zombie task handling | INTELLIGENT | Reasoner: release vs. extend timeout vs. kill |
| Model tier selection | INTELLIGENT | Reasoner: based on task complexity + failure history |

### 3.4 Act Phase

The Sentinel issues **commands** to the worker pool and other systems:

```python
@dataclass
class SentinelCommand:
    """A concrete action the Sentinel wants to take."""
    command_type: CommandType
    target: str              # task_id, project_id, etc.
    params: dict
    decision_id: str         # links back to the Decision that produced this
    requires_approval: bool  # supervised tier

class CommandType(str, Enum):
    # Worker pool commands
    DISPATCH_TASK = "dispatch_task"         # send task to worker
    CANCEL_TASK = "cancel_task"             # abort running task

    # State transitions
    COMPLETE_PROJECT = "complete_project"
    FAIL_PROJECT = "fail_project"
    PAUSE_PROJECT = "pause_project"
    RESUME_PROJECT = "resume_project"
    ADVANCE_WAVE = "advance_wave"

    # Strategy adjustments
    REASSIGN_TIER = "reassign_tier"         # change model for task
    ADJUST_CONCURRENCY = "adjust_concurrency"
    UPDATE_BUDGET = "update_budget"

    # Task management
    RETRY_TASK = "retry_task"
    SKIP_TASK = "skip_task"
    RELEASE_CLAIM = "release_claim"
    REQUEUE_WITH_CONTEXT = "requeue_with_context"  # retry with diagnostic info

    # Planning
    REPLAN_TASK = "replan_task"             # decompose or rewrite task
    SPLIT_TASK = "split_task"              # break into subtasks
    MERGE_TASKS = "merge_tasks"            # combine related tasks

    # Verification/Review
    VERIFY_OUTPUT = "verify_output"
    REQUEST_REVIEW = "request_review"
    ACCEPT_OUTPUT = "accept_output"

    # User interaction
    PROPOSE_TO_USER = "propose_to_user"    # supervised decision
    NOTIFY_USER = "notify_user"            # informational
```

---

## 4. Data Flow

### 4.1 Project Execution Flow (Target)

```
API Request: POST /api/projects/{id}/execute
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  SYSTEM SENTINEL                                          │
│  • Receives execution request                             │
│  • Spawns ProjectSentinel for this project                │
│  • Allocates worker pool slots                            │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│  PROJECT SENTINEL (per-project brain)                     │
│                                                           │
│  1. Initialize world model from DB                        │
│  2. Load execution strategy (from plan config)            │
│  3. Enter decision loop:                                  │
│     ┌─────────────────────────────────────────────┐      │
│     │ OBSERVE: poll world model for changes        │      │
│     │ ORIENT:  evaluate rules on current state     │      │
│     │ DECIDE:  mechanical → auto; intelligent → LLM│      │
│     │ ACT:     issue commands to worker pool        │      │
│     │          persist decision + reasoning to DB   │      │
│     └─────────────────────────────────────────────┘      │
│                                                           │
│  4. On terminal state → final sweep → self-terminate      │
└─────────────┬──────────────────────┬─────────────────────┘
              │ dispatch commands     │ results
              ▼                       │
┌─────────────────────────────┐      │
│  WORKER POOL (executor)      │      │
│                              │      │
│  • Receives dispatch command │      │
│  • Runs task in sandbox      │──────┘
│  • Streams progress events   │
│  • Reports final result      │
│  • No retry logic            │
│  • No wave awareness         │
│  • No budget decisions       │
└─────────────────────────────┘
```

### 4.2 Task Dispatch Flow (Target)

```
ProjectSentinel                    Worker Pool
      │                                │
      │  ── DISPATCH_TASK ──────────→  │
      │     { task_id, tier,           │
      │       context, timeout,        │
      │       budget_limit }           │
      │                                │
      │  ←── task_started ──────────── │
      │     { task_id, worker_id,      │
      │       started_at }             │
      │                                │
      │  ←── task_progress ─────────── │  (periodic)
      │     { task_id, output_delta,   │
      │       tokens_used }            │
      │                                │
      │  ←── task_completed ────────── │  OR
      │     { task_id, output,         │
      │       tokens_used, duration,   │
      │       files_changed }          │
      │                                │
      │  ←── task_failed ──────────── │
      │     { task_id, error,          │
      │       error_type, partial }    │
      │                                │
      │  (Sentinel decides next step)  │
      │  ── VERIFY_OUTPUT ──────────→  │  (optional: sentinel-commanded)
      │  ←── verification_result ───── │
      │                                │
      │  ── DISPATCH_TASK (next) ───→  │
      │     ...                        │
```

### 4.3 Failure Handling Flow (Target)

```
Worker reports task_failed
        │
        ▼
ProjectSentinel.observe()
  → updates world_model.failed[task_id]
        │
        ▼
ProjectSentinel.orient()
  → check_cascade_failure() returns observation
        │
        ▼
ProjectSentinel.decide()
  │
  ├─ retry_count < max AND reasoner says "transient"
  │   → Decision(RETRY_TASK, AUTO, "transient error, retrying with backoff")
  │
  ├─ retry_count < max AND reasoner says "context issue"
  │   → Decision(REQUEUE_WITH_CONTEXT, AUTO, "adding diagnostic context")
  │
  ├─ retry_count < max AND reasoner says "wrong tier"
  │   → Decision(REASSIGN_TIER, AUTO, "upgrading from haiku to sonnet")
  │
  ├─ retries exhausted AND reasoner confidence > 0.7
  │   → Decision(SKIP_TASK, AUTO, "task unrecoverable, unblocking deps")
  │
  ├─ retries exhausted AND reasoner confidence < 0.5
  │   → Decision(SKIP_TASK, SUPERVISED, "uncertain — asking user")
  │
  └─ cascade detected (3+ failures)
      → Decision(PAUSE_PROJECT, SUPERVISED, "cascade failure, recommending pause")
        │
        ▼
ProjectSentinel.act()
  → issue command
  → persist decision to DB with full reasoning chain
  → publish event for UI
```

---

## 5. Execution Strategy

The Sentinel maintains a mutable **ExecutionStrategy** that governs its decision-making. This replaces the scattered config flags in the current executor.

```python
@dataclass
class ExecutionStrategy:
    """Mutable strategy that the Sentinel adjusts during execution."""

    # Concurrency
    max_parallel_tasks: int = 3
    max_parallel_per_tier: dict[ModelTier, int] = field(default_factory=lambda: {
        ModelTier.OLLAMA: 2,
        ModelTier.HAIKU: 3,
        ModelTier.SONNET: 2,
        ModelTier.OPUS: 1,
    })

    # Retry policy
    max_retries_per_task: int = 3
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    retry_with_diagnostic: bool = True

    # Verification
    verification_enabled: bool = True
    verification_skip_tiers: set[ModelTier] = field(default_factory=lambda: {ModelTier.OLLAMA})
    review_enabled: bool = False
    review_max_iterations: int = 3

    # Budget
    budget_limit: float | None = None
    budget_pause_threshold: float = 0.95   # pause at 95% spent
    budget_downgrade_threshold: float = 0.80  # consider tier downgrade at 80%

    # Wave management
    wave_checkpoints: bool = False
    wave_pr_on_complete: bool = False

    # Model selection
    allow_tier_downgrade: bool = True  # auto-downgrade on repeated failures
    allow_tier_upgrade: bool = False   # auto-upgrade for complex tasks

    # Confidence thresholds
    auto_threshold: float = 0.6        # above = auto-execute, below = supervised

    # Debug/control
    step_mode: bool = False            # pause after each decision
    dry_run: bool = False              # log decisions but don't execute
    trace_reasoning: bool = False      # include full LLM reasoning in logs
```

---

## 6. Internal Communication

### 6.1 Sentinel ↔ Worker Pool Protocol

The communication between Sentinel and Worker Pool uses an **in-process async channel** (not REST). This eliminates the current indirection where the Sentinel sends REST requests to the same process.

```python
class WorkerPool:
    """Manages a pool of task executors. No decision-making."""

    def __init__(self, max_workers: int, resource_monitor: ResourceMonitor):
        self._semaphores: dict[str, asyncio.Semaphore]  # per-tier limits
        self._active: dict[str, WorkerTask]              # task_id → running task
        self._event_queue: asyncio.Queue[WorkerEvent]    # results back to sentinel

    async def dispatch(self, command: DispatchCommand) -> None:
        """Accept a dispatch command. Non-blocking. Results come via event_queue."""
        # Acquire semaphore slot for tier
        # Spawn asyncio task to run executor
        # Worker publishes events to _event_queue

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if cancelled."""

    async def events(self) -> AsyncIterator[WorkerEvent]:
        """Yield worker events as they arrive."""

    @property
    def available_slots(self) -> dict[str, int]:
        """Current availability by tier."""

@dataclass
class DispatchCommand:
    task_id: str
    project_id: str
    tier: ModelTier
    prompt: str                    # fully assembled prompt
    context: str | None            # enriched context
    timeout_seconds: int = 600
    budget_limit: float | None = None
    tools: list[str] | None = None

@dataclass
class WorkerEvent:
    event_type: str          # task_started | task_progress | task_completed | task_failed
    task_id: str
    project_id: str
    timestamp: datetime
    data: dict               # type-specific payload
```

### 6.2 Sentinel ↔ User Protocol

The Sentinel is the **user-facing controller**. The extension talks to the Sentinel, not the executor.

```
Extension (VSCode)                     Sentinel
      │                                    │
      │  ── SSE: /sentinel/stream ───────→ │  (subscribe to project events)
      │  ←── decisions, proposals, status   │
      │                                    │
      │  ── POST /sentinel/approve ──────→ │  (approve supervised decision)
      │  ── POST /sentinel/reject ───────→ │  (reject, sentinel re-reasons)
      │  ── POST /sentinel/pause ────────→ │  (pause execution)
      │  ── POST /sentinel/resume ───────→ │  (resume with optional strategy change)
      │  ── POST /sentinel/override ─────→ │  (force a specific decision)
      │  ── POST /sentinel/inject ───────→ │  (inject synthetic event for debugging)
      │  ── GET  /sentinel/decisions ────→ │  (audit trail of all decisions)
      │  ── GET  /sentinel/strategy ─────→ │  (current execution strategy)
      │  ── PUT  /sentinel/strategy ─────→ │  (update strategy mid-execution)
      │                                    │
```

---

## 7. Decision Persistence

Every decision the Sentinel makes is persisted with full context for auditability.

```python
@dataclass
class DecisionRecord:
    id: str                          # uuid
    project_id: str
    task_id: str | None              # if task-specific
    timestamp: datetime

    # What triggered this decision
    trigger: str                     # observation category or event type
    trigger_details: dict            # full observation/event data

    # World state at decision time
    world_snapshot: dict             # serialized ProjectWorldModel subset

    # The decision
    decision_type: DecisionType      # MECHANICAL or INTELLIGENT
    action: CommandType
    params: dict
    tier: DecisionTier               # AUTO or SUPERVISED

    # Reasoning (for intelligent decisions)
    reasoning: str | None            # LLM explanation
    confidence: float | None
    supporting_evidence: list[str] | None
    alternatives_considered: list[dict] | None  # what else was considered

    # Outcome (filled in after execution)
    outcome: str | None              # success, failed, overridden, rejected
    outcome_details: dict | None
```

Stored in SQLite (`sentinel_decisions` table) alongside existing `sentinel_observations`. Optionally forwarded to the context store for semantic search in future reasoning.

---

## 8. Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATION SERVER                       │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                    SYSTEM SENTINEL                           │ │
│  │  • Fleet-level resource monitoring                          │ │
│  │  • Cross-project contention detection                       │ │
│  │  • ProjectSentinel lifecycle management                     │ │
│  └────────┬────────────────────────────────────────────────────┘ │
│           │ spawns / manages                                      │
│           ▼                                                       │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │               PROJECT SENTINEL (per-project)                 │ │
│  │                                                              │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐ │ │
│  │  │  World   │  │  Rules   │  │ Decision │  │  Command   │ │ │
│  │  │  Model   │→ │ Engine   │→ │ Engine   │→ │ Dispatcher │ │ │
│  │  │          │  │          │  │          │  │            │ │ │
│  │  │ • tasks  │  │ • ready  │  │ • mech.  │  │ • dispatch │ │ │
│  │  │ • waves  │  │ • stuck  │  │ • reason │  │ • cancel   │ │ │
│  │  │ • budget │  │ • done   │  │ • escal. │  │ • state    │ │ │
│  │  │ • health │  │ • dead   │  │ • log    │  │ • notify   │ │ │
│  │  └──────────┘  └──────────┘  └──────────┘  └─────┬──────┘ │ │
│  │                                                    │        │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │        │ │
│  │  │ Reasoner │  │ Strategy │  │ Decision Store   │ │        │ │
│  │  │ (LLM)    │  │ (mutable)│  │ (SQLite+CtxStore)│ │        │ │
│  │  └──────────┘  └──────────┘  └──────────────────┘ │        │ │
│  └───────────────────────────────────────────────────┼────────┘ │
│                                                       │          │
│           commands ↓                    ↑ events      │          │
│                                                       │          │
│  ┌────────────────────────────────────────────────────┼────────┐ │
│  │                    WORKER POOL                      │        │ │
│  │                                                     │        │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐         │        │ │
│  │  │ Worker 1 │  │ Worker 2 │  │ Worker N │  ←──────┘        │ │
│  │  │ (claude) │  │ (ollama) │  │ (gemini) │                  │ │
│  │  └──────────┘  └──────────┘  └──────────┘                  │ │
│  │                                                             │ │
│  │  ┌──────────────────────────────────────┐                  │ │
│  │  │ Resource Monitor                      │                  │ │
│  │  │ (health probes, slot tracking)        │                  │ │
│  │  └──────────────────────────────────────┘                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                    REST/SSE LAYER                            │ │
│  │  • /sentinel/* — all orchestration control                  │ │
│  │  • /workers/* — worker pool status (read-only)              │ │
│  │  • /projects/* — CRUD only (no execution logic)             │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. State Machine: Project Lifecycle (Sentinel-Owned)

```
                    POST /projects/{id}/execute
                              │
                              ▼
                     ┌────────────────┐
                     │   INITIALIZING  │
                     │                 │
                     │ • load plan     │
                     │ • build graph   │
                     │ • init strategy │
                     └────────┬───────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │          EXECUTING             │
              │                                │
              │  Sentinel decision loop active  │
              │  Worker pool dispatching tasks  │
              └──┬──────────┬──────────┬──────┘
                 │          │          │
    budget/user  │  all ok  │  failure │  user override
    pause        │          │  cascade │
                 │          │          │
                 ▼          ▼          ▼
          ┌──────────┐ ┌────────┐ ┌──────────┐
          │  PAUSED   │ │COMPLET.│ │  FAILED   │
          │           │ │        │ │           │
          │ • reason  │ │ • final│ │ • reason  │
          │ • can     │ │   sweep│ │ • partial │
          │   resume  │ │ • PR   │ │   results │
          └─────┬─────┘ └────────┘ └───────────┘
                │
                │ user resume / budget added
                ▼
           (back to EXECUTING)
```

---

## 10. Key Design Principles

### 10.1 Separation of Concerns

| Concern | Owner | Why |
|---------|-------|-----|
| "What to do next" | Sentinel | Requires reasoning about state, history, strategy |
| "How to do it" | Worker Pool | Mechanical execution, tool invocation |
| "Is it good enough" | Sentinel | Verification is a quality decision, not execution |
| "Should we retry" | Sentinel | Retry strategy depends on error type, budget, history |
| "What tier to use" | Sentinel | Model selection depends on task complexity + failure patterns |
| "When to stop" | Sentinel | Completion detection requires understanding of goals |

### 10.2 Eventual Consistency

The Sentinel's world model is **eventually consistent** with the DB. Worker events update the model first, then the Sentinel issues DB writes. This means:

- The Sentinel always acts on the freshest data (no stale DB reads in hot path)
- DB state may lag behind Sentinel state by one tick
- On Sentinel crash, the DB is the source of truth for recovery
- Recovery: load world model from DB, resume decision loop

### 10.3 Graceful Degradation

| Component Down | Behavior |
|----------------|----------|
| LLM Reasoner unavailable | Fall back to rule-only decisions (all intelligent → mechanical with conservative defaults) |
| Context Store unavailable | Skip historical lookup, reason without past incidents |
| Worker offline for a tier | Sentinel adjusts strategy: reassign tasks to available tiers |
| Budget API unreachable | Pause dispatching, resume when reachable |
| Sentinel crash | Worker pool drains active tasks, system sentinel detects absence, restarts |

### 10.4 Auditability

Every decision the Sentinel makes is:
1. **Logged** — with timestamp, trigger, world state snapshot, reasoning, action
2. **Persisted** — to SQLite and optionally context store
3. **Streamable** — via SSE to the extension for real-time display
4. **Queryable** — via REST API for post-execution analysis
5. **Linked** — decision → observation → task → project chain

---

## 11. Verification and Review as Sentinel-Controlled Post-Processing

Today, verification and review are embedded in `task_lifecycle.py` and run synchronously after task execution. In the target architecture, they become **Sentinel-commanded operations**:

```
Task completes in worker pool
        │
        ▼
Sentinel receives task_completed event
        │
        ▼
Sentinel decides: should this be verified?
  (based on: strategy, tier, task importance, budget remaining)
        │
        ├─ Yes → VERIFY_OUTPUT command to worker pool
        │         Worker runs verification prompt
        │         Returns verification_result event
        │         Sentinel decides: accept / retry / escalate
        │
        └─ No → Accept output, advance to next task
```

This gives the Sentinel control over the full quality pipeline — it can skip verification for low-risk tasks, run extra verification for critical ones, or adjust verification rigor based on remaining budget.

---

## 12. Summary of Architectural Changes

| Aspect | Current | Target |
|--------|---------|--------|
| **Tick loop location** | `executor.py` | `ProjectSentinel` decision loop |
| **Wave dispatch** | Executor finds ready tasks | Sentinel evaluates `check_tasks_ready()` rule |
| **Retry logic** | `task_lifecycle.py` catch blocks | Sentinel `decide()` on `task_failed` event |
| **Budget control** | Executor `_check_budget()` | Sentinel strategy + budget rules |
| **Resource checks** | Executor `_resources_available()` | Worker Pool `ResourceMonitor` reports to Sentinel |
| **Completion detection** | Executor end-of-tick | Sentinel `check_project_complete()` rule |
| **Zombie detection** | Executor `_recover_zombie_tasks()` | Sentinel `check_task_stuck()` rule (already exists) |
| **Verification** | task_lifecycle inline | Sentinel-commanded post-processing |
| **Review cycle** | task_lifecycle inline | Sentinel-commanded post-processing |
| **Model selection** | Static per-task from plan | Sentinel dynamic reassignment |
| **User interaction** | Extension → executor endpoints | Extension → sentinel endpoints |
| **Decision persistence** | None (fire-and-forget) | Every decision logged with reasoning |
| **Concurrency control** | Semaphore in executor | Sentinel strategy + worker pool slots |

---

# Sentinel: New Capabilities, API Surface & MCP

This document specifies the new capabilities of the Sentinel as the orchestration brain, its API surface for external communication, and the MCP tools for direct interaction.

## 1. New Capabilities

The Sentinel's new role as the primary orchestrator introduces a range of capabilities for intelligent, dynamic control over the project lifecycle.

### 1.1. Planning Control

The Sentinel will have the authority to dynamically modify the project plan in response to real-time events, such as task failures, new information, or user directives.

-   **Re-planning:** Upon significant task failure or deviation, the Sentinel can trigger a re-planning phase. It will use the original plan, the current project state, and the failure context to generate a revised plan.
-   **Rigor Adjustment:** The Sentinel can adjust the rigor of task verification. For instance, if a set of tasks is consistently failing, it can increase the rigor for subsequent tasks in that wave, requiring more stringent verification or even human-in-the-loop (HITL) approval. Conversely, it can decrease rigor for tasks that are consistently succeeding.
-   **Task Splitting/Merging:** The Sentinel can decompose a complex, failing task into smaller, more manageable sub-tasks. It can also merge simple, related tasks into a single unit of work to optimize overhead.

### 1.2. Model Selection Control

The Sentinel will manage model assignments for tasks, allowing for dynamic adjustments based on performance and cost.

-   **Tier Reassignment:** If a task fails due to model limitations (e.g., context window, reasoning capability), the Sentinel can automatically re-dispatch the task to a higher-tier model.
-   **Cost Optimization:** For simple, repetitive tasks, the Sentinel can default to a lower-cost model. It will monitor success rates and switch to a higher-tier model only if necessary.
-   **Failure Pattern Analysis:** The Sentinel will track failure patterns associated with specific models and tasks. This data will inform its model selection strategy over time, building a heuristic for which model is best suited for a given task type.

### 1.3. Parallelism Control

The Sentinel will manage the concurrency of the executor worker pool to optimize for speed, cost, and resource utilization.

-   **Dynamic Concurrency:** The Sentinel will monitor the performance of the worker pool and the underlying system resources. It can increase or decrease the number of concurrent tasks to maintain optimal throughput without overloading the system.
-   **Resource-based Throttling:** If tasks require access to a shared, limited resource (e.g., a specific API, a database connection pool), the Sentinel can limit the number of concurrent tasks that access that resource.
-   **Dependency-aware Dispatch:** The Sentinel will analyze the task dependency graph to dispatch independent tasks in parallel, maximizing utilization of the worker pool.

### 1.4. Debug & Control Flags

The Sentinel will expose a set of flags for fine-grained control and debugging of the orchestration process. These flags can be set via the API or MCP tools.

-   **Pause/Resume:** Halt all orchestration activities, allowing for inspection of the current state. The system can be resumed from the point it was paused.
-   **Step-Through:** Execute one task or one decision loop iteration at a time, requiring explicit user approval to proceed to the next step.
-   **Event Injection:** Manually inject events into the Sentinel's event bus to test its response to specific scenarios (e.g., a simulated task failure, a new user request).
-   **Decision Override:** Intercept a decision from the Sentinel's reasoner and provide a different outcome. For example, forcing the selection of a specific model for a task.

### 1.5. State Persistence & Auditability

Every decision made by the Sentinel will be persisted to the database for auditability and traceability.

-   **Decision Log:** A new table, `sentinel_decisions`, will store a log of every decision made by the Sentinel, including the input state, the reasoning process, and the resulting command.
-   **State Snapshots:** The Sentinel will periodically snapshot the entire project state, providing a historical record that can be used for debugging and analysis.
-   **Audit Trail:** The decision log will serve as an audit trail, allowing developers and users to understand why the Sentinel took a specific action at a given point in time.

## 2. API Surface

The Sentinel's new role requires a dedicated API surface for communication with the extension and other external systems. The primary communication channel will be a set of RESTful endpoints.

### 2.1. Endpoints

-   `POST /sentinel/command`: The main endpoint for interacting with the Sentinel. The request body will contain the command and its parameters.
    -   **Commands:** `pause`, `resume`, `step`, `set_flag`, `inject_event`, `override_decision`.
    -   **Example:** `POST /sentinel/command` with body `{"command": "set_flag", "payload": {"name": "pause_on_failure", "value": true}}`

-   `GET /sentinel/state`: Retrieve the current state of the orchestration process, including the project plan, task status, and active flags.

-   `GET /sentinel/decisions`: Query the decision log, with support for filtering by time, task, and decision type.

-   `GET /sentinel/history`: Retrieve historical state snapshots.

### 2.2. Extension Communication

The VS Code extension will communicate directly with the Sentinel via these new endpoints. It will no longer interact with the Executor's API for orchestration control. The extension will use the `/sentinel/state` endpoint to update its UI and will send user actions (e.g., pausing the process, overriding a decision) to the `/sentinel/command` endpoint.

## 3. MCP Tools

A new set of MCP (Mission Control Protocol) tools will be created for direct, command-line interaction with the Sentinel.

-   `sentinel-cli`: A command-line interface for interacting with the Sentinel's API.
    -   `sentinel-cli state`: Get the current state.
    -   `sentinel-cli pause`: Pause the orchestration process.
    -   `sentinel-cli resume`: Resume the orchestration process.
    -   `sentinel-cli step`: Execute the next step.
    -   `sentinel-cli set-flag <flag_name> <value>`: Set a debug/control flag.
    -   `sentinel-cli inject-event <event_type> <payload>`: Inject an event.
    -   `sentinel-cli decisions --since=1h`: View recent decisions.

This comprehensive set of capabilities, APIs, and tools will empower the Sentinel to act as the intelligent core of the Hekate orchestration system, providing robust, flexible, and observable control over the entire project lifecycle.

---

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
