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
