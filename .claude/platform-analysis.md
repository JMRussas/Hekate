# Hekate Platform Analysis
**Date:** 2026-03-31
**Scope:** Full codebase + execution history + infrastructure analysis
**Codebase:** ~353 Python files, ~15K lines gods pipeline, ~12K lines orchestration services

---

## 1. Architecture Health

The event-driven pipeline design is **architecturally sound**. The relay table as durable event log, cursor-based restart recovery, idempotency keys, and handler isolation are well-designed patterns inspired by Netflix Conductor.

### Strengths
- Clean separation: each god handles one concern (plan, dispatch, execute, verify, git, budget, context)
- Event-driven with durable relay table — no in-memory-only state
- Idempotency keys via SHA256 hash + UNIQUE constraint prevent duplicate processing
- Smart restart: resumes from MAX(id)-50, avoids replaying entire history
- Dual SQLite/Postgres backend with clean abstraction and automatic placeholder translation ($N → ?)
- Atomic state transitions: `transition_and_emit()` updates task status AND emits events in one transaction
- DI container pattern in orchestration (testable, modular)
- Circuit breaker on context store client (5 failures → 60s cooldown)
- LLM fallback chains: gateway → direct CLI → next provider

### Coupling Points (Scale Blockers)
1. **Single-threaded event loop** — pipeline.tick() is sequential. At 100+ concurrent tasks, handler execution time dominates tick duration
2. **O(n²) event deduplication** — relay dedup scans all events per tick. At 10K events/day, this becomes expensive
3. **Gateway as single point of failure** — Odin blocks dispatch if /providers fails. No cached fallback
4. **Context store API search is O(n*m)** — linear scan through all plans × all tasks per task_verified event. N+1 attribute subqueries per node fetch
5. **SQLite write contention** — WAL helps but single-writer lock under high concurrency will bottleneck. No connection pooling in run_pipeline.py
6. **Deferred queues unbounded** — both Hermes and Mimir queue without max size, risking OOM under burst load
7. **No backpressure** — if dispatch rate >> execution rate, deferred lists grow indefinitely

### Component Map

```
┌──────────────────────────────────────────────────────────┐
│                     MCP Bridges (SSE)                     │
│  Prometheus (5212)  Hades (5211)  AgentContext (5213)    │
└─────────────┬──────────┬──────────────┬──────────────────┘
              ↓          ↓              ↓
┌─────────────────────────────────────────────────────────┐
│                    Routes Layer                          │
│  projects │ tasks │ external │ auth │ chat │ odin │ ... │
└─────────────────────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────────────────────┐
│                  Services Layer                          │
│  planner → decomposer → executor ← task_lifecycle       │
│                         ↓                               │
│  model_router ← llm_router → LLM Gateway (5210)        │
│       ↓                          ↓                      │
│  tree_runner, odin, chat_agent, context_store_client    │
└─────────────────────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────────────────────┐
│                Gods Pipeline (Event Loop)                │
│  Athena → Odin → Hermes → Mimir → Hephaestus → Tyche   │
│  relay table │ cursor │ gates │ idempotency             │
└─────────────────────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────────────────────┐
│                  Data Layer                              │
│  Postgres (asyncpg) or SQLite (aiosqlite, WAL)          │
│  Context Store (Postgres + AGE + pgvector, port 5433)   │
└─────────────────────────────────────────────────────────┘
```

---

## 2. Code Quality

### Codebase Statistics

| Metric | Value | Assessment |
|--------|-------|------------|
| Python files (source) | 353 | Medium-large codebase |
| Test files | 291 | Comprehensive coverage |
| TODO/FIXME/HACK comments | 2 | Extremely clean |
| Bare `except:` clauses | 0 | Professional |
| Broad `except Exception:` | 107 | Defensive, appropriate |
| Hardcoded Windows paths | ~10 | All env-var backed with defaults |
| `print()` calls | 279 | Confined to tests/tools (proper `logging` in production) |
| Type hint coverage | ~50% | Strong in handlers/models, weak in routes |

### Systematic Patterns

| Pattern | Instances | Risk |
|---------|-----------|------|
| Silent error swallowing (except: pass/warning) | 15+ | Data loss, invisible failures |
| Soft state machine enforcement (log + allow invalid transitions) | 6 | State corruption |
| Row polymorphism without type guards (_val, isinstance checks) | 20+ | Wrong column unpacking |
| JSON parsing without schema validation | 10+ | Silent data degradation |
| Hardcoded timeouts (30s git, 30min gateway, 600s cold start) | 5 | No per-request tuning |
| Duplicated logic (dedup in pipeline + relay, row unpacking in 4 gates) | 3 | Inconsistent fixes |
| No connection pooling (MCP bridges, context store MCP, run_pipeline) | 4 | Connection churn |

### Dead Code
- `dispatch.py:518`: Budget sensitivity code unreachable — `budget_remaining` parameter never passed
- `dispatch.py`: `diagnose_failure()` imported but never called in production
- Tyche budget gate: `tyche_check_budget()` defined but NOT registered — budget tracked but never enforced
- Sentinel route (`sentinel.py`): Legacy, replaced by Odin. Still deployed and wired in router
- Commented sections in `review.py`, `plan_rules.py`, `dispatch.py` — experimental code left in place

### Logging Quality
- **Consistent**: `logger = logging.getLogger(__name__)` across 80+ files
- **Proper levels**: DEBUG/INFO/WARNING/ERROR used correctly
- **Blind spots**:
  - Relay HTTP sink logs connection errors at DEBUG only
  - Cursor persistence swallows all exceptions
  - Payload JSON wrapping (non-dict → `{"raw": ...}`) happens without logging
  - Knowledge extraction failures logged as warning only, no retry

### Subprocess Management
- 50+ files use subprocess — all with proper async, timeouts, and cleanup
- No fire-and-forget processes
- Process.kill() on timeout with proper exception handling
- Git operations isolated per project via worktrees

---

## 3. Handler Assessment

### Athena (Planning) — athena_leveled.py (883 lines)
**Job:** Two-model planning (generator + reviewer), L1→L2→L3 deepening, TDD phase
**Quality:** Good architecture, several fragility points

| Aspect | Status |
|--------|--------|
| Two-model review (Model A generates, Model B critiques) | Working |
| Rule validation with retry (MAX_RULE_RETRIES=2) | Working |
| Review cycles (MAX_REVIEW_CYCLES=2) | Working |
| Node tree planner (L0 parallel, self-filtering) | Working |
| Completion propagation (bubble_up, materialize) | Working |

**Failure modes:**
- CLI planner fails → silently falls back to gateway (loses code analysis via hekate-mcp)
- JSON parse fails → creates placeholder plan with empty phases → decomposition produces 0 tasks → project fails
- Rule validation retries exhausted → reverts to "last good plan" which may not exist on first attempt
- All narration lost if handler crashes before returning (no checkpoint)

### Odin (Orchestration) — odin.py (796 lines)
**Job:** Dispatch ready tasks, manage lifecycle, unblock dependencies, diagnose failures
**Quality:** Most complex handler, handles many edge cases

**Failure modes:**
- Provider availability check fails → ALL providers marked down → dispatch blocked entirely (fail-conservative, no cache)
- Deadlock detection marks project failed when blocked > 0 and no running tasks, but doesn't check needs_review
- Fork with empty items emits task_fork_requested with zero items
- Idempotency key for project events uses task_count — could collide on re-run with same count

### Hermes (Execution) — hermes_async.py (800 lines)
**Job:** Async CLI execution with concurrency control, deferred queue, heartbeat
**Quality:** Solid async architecture, good timeout handling

| Feature | Status |
|---------|--------|
| Async subprocess with timeout | Working |
| Concurrency control (asyncio.Lock, serialized check-and-register) | Working |
| Deferred queue (FIFO) | Working, unbounded |
| Heartbeat (30s default) | Working |
| Worktree isolation | Working, collision risk |

**Failure modes:**
- Worktree name derived from project name slug — similar names could conflict (should use project_id hash)
- Deferred queue is FIFO without priority — high-priority tasks wait behind low-priority
- Relay write failure logged as CRITICAL but no remediation — tasks stuck in running state
- Heartbeat tasks created per execution with no cleanup timeout

### Mimir (Verification) — mimir.py (842 lines)
**Job:** Heuristic + LLM verification, code review, knowledge extraction
**Quality:** Good separation of concerns

**Failure modes:**
- Error pattern regex misses non-standard error messages
- Calls both gateway AND engine /verify — if /verify fails, gateway result is lost
- Knowledge extraction failure silently ignored — findings lost permanently
- Deferred verification list unbounded — burst of 100 completions could exhaust memory
- Drain runs in background — wave_complete could fire before drain finishes

### Hephaestus (Git) — hephaestus.py (313 lines)
**Job:** Syntax check, git stage, commit, push, PR creation
**Quality:** Functional but fragile error handling

**Failure modes:**
- Branch creation failure → silently falls back to current branch → commits to main
- PR creation failure logged but ignored → project_committed event emitted with fabricated state
- Git timeout hardcoded to 30s — large pushes may need more
- Empty affected_files set → git add with no files specified

### Tyche (Budget) — tyche.py (80 lines)
**Job:** Budget tracking (NOT enforcement)
**Quality:** Incomplete

**Critical gap:** Budget check defined but NOT registered as dispatch gate. All tasks dispatch regardless of budget. Cost tracking only happens AFTER completion — no pre-dispatch budget check. Neural Roguelike ran $20.28 unchecked.

### Context Bridge — context_bridge.py
**Job:** Sync pipeline events to context store knowledge graph
**Quality:** Working but fragile

**Failure modes:**
- Global mutable `_cs_project_id` cached without invalidation
- Context store API response format varies (3 different shapes)
- Task node search is O(n*m) — no indexed query
- Knowledge findings orphaned if task node not found

---

## 4. Database

### Schema
- **30 migrations** in orchestration, all following NNN naming convention
- **Core tables:** users, projects, plans, tasks, task_deps, usage_log, checkpoints, god_relay_events, odin_decisions, request_log, fix_queue, plan_nodes
- **Dual backend:** Postgres (asyncpg) via ORCHESTRATION_DSN, SQLite (aiosqlite, WAL mode) fallback

### Gaps
- No explicit dispatch tracking table — state inferred from task.status + model_tier
- No rate_limit tracking table — only in TaskDefinition config
- Provider field is NULL for all 286 tasks — provider tracking not wired into context_json
- Worktree paths not stored in context_json despite being used
- Source and deployment databases diverge — deployment has 67 projects vs source's 52
- Schema duplication: Migration 027 creates god_relay_events, but run_pipeline.py also has CREATE TABLE IF NOT EXISTS for same table
- No connection pooling in run_pipeline.py — single aiosqlite connection shared across all handlers

### Context Store DB (Postgres + AGE + pgvector)
- Nodes with typed attributes, parent-child hierarchy
- pgvector cosine similarity for semantic search
- AGE graph for temporal edge history
- **N+1 problem:** each node fetch queries attributes separately (not batched)
- **No connection pooling** in MCP server — opens/closes per tool call
- Fire-and-forget embeddings — node N+1 may not see node N's embedding

---

## 5. Provider System

### Status

| Provider | Status | Notes |
|----------|--------|-------|
| Claude Code CLI | **Active** | Primary. stream-json mode. Cold start 60-120s. Auth via flat file, works under NSSM |
| Ollama | **Active** | Local, free. Used for asset tasks only |
| Gemini CLI | **Broken** | Exit code 1, auth issue. All tasks route to claude_code |
| Codex CLI | **Broken** | ChatGPT subscription issue. Disabled |

### Provider Architecture
- Clean base class: `build_command()`, `build_env()`, `parse_output()`, `parse_error()`
- Per-line inactivity timeouts (600s cold start, 300s active)
- Error classification: auth_error, rate_limit, context_overflow, nested_session
- 10MB buffer limit prevents memory issues
- GeminiPool serializes requests with single `_request_lock` — would bottleneck if re-enabled

### Model Routing
- Tier map in `odin.py` (`_TIER_MAP`): task_type + complexity → provider
- Fallback chain: `[claude_code, ollama]`
- Routing strategies: best/cheapest/balanced (configurable)
- Dynamic model discovery with caching
- Learning integration: can avoid historically-failing models

### Missing
- No overall task timeout enforcement — TaskDefinition has `response_timeout_seconds` but no background monitor
- Worktree names use project slug (collision risk) — should use project_id hash

---

## 6. Workflow Primitives

**Status: Infrastructure complete, usage zero.**

| Primitive | Code | Schema | Data | Tests | Production |
|-----------|------|--------|------|-------|------------|
| DECISION | Full handler with rule + LLM evaluation | workflow_edges table | Empty | None | Never used |
| FORK | Full handler with fan-out + sub-task creation | workflow_edges + fork_group_id | Empty | None | Never used |
| JOIN | Threshold-aware dependency checking | Join mode in task deps | Empty | None | Never used |

**Root cause:** No INSERT INTO workflow_edges exists anywhere in the codebase. Athena reads the table but never writes to it. No API endpoint creates edges. Plans have no branching syntax. The entire infrastructure is a scaffold without data.

### Task Definition Registry
- Conductor-inspired: retry (FIXED/EXPONENTIAL_BACKOFF/LINEAR_BACKOFF), timeout, rate limiting, concurrency, human tasks
- **Enforcement:** Soft. Decomposer checks task types against registry. Missing definitions fall back to hardcoded defaults. No hard constraint prevents unknown task types

### Durable Execution

| Feature | Status | Notes |
|---------|--------|-------|
| Event log (relay table) | Working | 7-day retention (hardcoded), cursor-based replay |
| Exactly-once (idempotency keys) | Working | SHA256 hash + UNIQUE constraint |
| Handler statelessness | Working | All state read from DB |
| Cursor persistence | Working | Persisted to god_registry after each tick |
| Atomic transitions | Working | transition_and_emit() for state + events |
| Event cleanup | Working | Every 100 ticks, delete events older than 7 days |
| Startup recovery | Missing | `run_startup_recovery()` import exists, function not implemented |
| Timeout enforcement | Missing | TaskDefinition has timeout_seconds but no background enforcer |
| Checkpoint replay | Partial | Cursor-based, not full event sourcing |

---

## 7. Execution Statistics

### Overall

| Metric | Source DB | Deployment DB |
|--------|----------|---------------|
| Total projects | 52 | 67 |
| Completed | 19 | ~25 |
| Failed | 1 | ~12 |
| Cancelled | 17 | ~20 |
| Draft | 7 | ~5 |
| Still executing | 0 | 2 |
| Total tasks | 188 | 286 |
| Task completion rate | 61.7% | 74.5% |
| Total spend | ~$5.50 | ~$31.67 |

### Notable Projects

| Project | Tasks | Completed | Cost | Avg/Task |
|---------|-------|-----------|------|----------|
| Neural Roguelike (noz-cs) | 22 | 18 | $20.28 | $1.19 |
| SluzzyGames: Server Test Coverage | 13 | 10 | $4.86 | $0.44 |
| Game Debug Architecture | 11 | 11 | $0.00* | — |
| Orchestration Postgres Migration | 8 | 8 | $0.41 | $0.05 |

*$0 cost = cost tracking wasn't wired during those runs.

### Success Metrics
- **Project success rate:** ~37% (25/67 deployment)
- **Task success rate:** 74.5% — once past planning, individual tasks mostly succeed
- **Cost per completed project:** ~$1.27 avg ($31.67 / 25). Highly skewed — excluding Neural Roguelike: ~$0.45/project
- **Sentinel era (legacy):** 100% completion within each project, $0.08-$0.18/task, very consistent

### God Relay Event Distribution (Deployment DB)

| Event | Count | Interpretation |
|-------|-------|----------------|
| heartbeat_tick | 33,771 | Pipeline alive and ticking continuously |
| review_passed | 1,646 | Mimir verification active |
| project_tick | 1,115 | Active project monitoring |
| project_complete | 541 | Completions (includes re-completions) |
| task_verified | 485 | Tasks going through verification |
| task_reset | 372 | Significant retry activity |
| dispatch_command | 136 | Tasks dispatched |
| slots_full | 113 | Concurrency limit hit regularly |
| planning_failed | 26 | Planning is #1 failure point |
| needs_human_review | 28 | Tasks escalated to human |
| handler_error | 30 | Handler crashes |

---

## 8. Failure Patterns

### 1. Planning Failures (26 events) — TOP FAILURE
Athena fails to generate valid plan JSON, or decomposition produces 0 tasks. Most newer projects fail here. JSON parse failures create placeholder plans with empty phases.

### 2. Project Re-creation Pattern
Users create v2, v3, v4 of same project after failure. GifterBoard had 5 attempts, SSE Health had 6 versions. No automatic retry-at-project-level exists.

### 3. Stuck Executing State
34 blocked + 33 pending tasks with no automatic cleanup. 2 projects still "executing." `run_startup_recovery()` is imported but not implemented.

### 4. Empty Task Output
Failed tasks show empty output_text. CLI execution starts but produces nothing. Heuristic detection exists in Mimir but doesn't prevent the failure.

### 5. Provider Tracking Gap
Provider field is NULL for all 286 tasks. Worktree paths not stored. Makes post-hoc failure diagnosis impossible.

### 6. Cold Start Latency
LLM Gateway first request after restart: 60-120s (Claude CLI auth). No warmup or connection pre-caching.

### 7. Budget Runaway
Neural Roguelike cost $20.28 with no enforcement. Tyche tracks but doesn't gate. No pre-dispatch budget check.

---

## 9. Infrastructure Assessment

### Service Registry (11 NSSM services)

| Service | Port | Runtime | Health Pattern | Status |
|---------|------|---------|----------------|--------|
| HekateEngine (gods + API) | 5200 | Python 3.11 | HTTP /api/health | Active |
| HekateContextStore | 5102 | .NET 8 | HTTP /api/health | Active |
| HekateServer (code analysis) | 5110 | .NET 8 | HTTP | Active |
| HekatePythonWorker | 9200 | .NET 8 | Internal | Active |
| HekateTypeScriptWorker | 9202 | .NET 8 | Internal | Active |
| HekateCppWorker | 9201 | .NET 8 | Internal | Active |
| HekateAdmin (Hades) | 5201 | Python 3.14 | HTTP | Active |
| HekateLLMGateway | 5210 | Python 3.11 | HTTP /health | Active |
| HekateHadesMcp | 5211 | Python 3.11 | SSE | Active |
| HekatePrometheusMcp | 5212 | Python 3.11 | SSE | Active |
| HekateAgentContextMcp | 5213 | Python 3.11 | SSE | Active |

### Deployment Pipeline (deploy.sh — 414 lines)
- **Strategy:** Stop → Build (staging) → Syntax check → Atomic swap → Start → Health check → Rollback on failure
- **Atomic swap:** staging → target, old → .old. Health failure triggers rollback
- **Fragility:** Hardcoded service names (11), hardcoded paths, migration sync uses file count only (no content comparison), npm output silenced, 20s total health check window

### MCP Bridge Pattern
All MCP bridges (Hades, Prometheus, Agent Context) use SSE transport with **no persistent connection** to backends. Each tool invocation creates a fresh HTTP client — no connection pooling. If backend is down, tools fail silently.

### Security
- Hades: No auth on any endpoint, CORS wide-open (`allow_origins=["*"]`), arbitrary command execution via /exec as LocalSystem
- LLM Gateway: Optional bearer tokens
- Prometheus MCP: Optional auth (admin@local.dev default)
- Context store MCP: Direct DB access (no auth layer)

---

## 10. Recommendations (Priority Order)

### P0 — Reliability
1. **Fix planning failures** — Better JSON validation, retry with feedback, structured output mode. This is the #1 failure cause (26 events)
2. **Implement startup recovery** — Re-trigger stuck RUNNING/DISPATCHED tasks on restart. Currently imported but not implemented
3. **Bound deferred queues** — Add max queue size to Hermes and Mimir to prevent OOM under burst

### P1 — Observability
4. **Wire provider tracking** — Store provider in context_json for post-hoc analysis (currently NULL for all 286 tasks)
5. **Store worktree paths** — Persist in context_json for failure diagnosis
6. **Add task timing** — planning_duration, execution_duration columns for performance analysis

### P2 — Cost Control
7. **Enforce budget gates** — Wire Tyche into dispatch registration to prevent runaway costs
8. **Pre-dispatch budget check** — Gate function before Hermes receives dispatch_command

### P3 — Resilience
9. **Add project-level retry** — Automatic re-plan on planning_failed instead of manual v2/v3/v4
10. **Connection pooling** — For run_pipeline.py, MCP bridges, and context store MCP server
11. **Provider availability cache** — Cache last-known provider state to survive transient /providers failures

### P4 — Architecture
12. **Implement workflow primitives** — Wire FORK/JOIN/DECISION with actual data. Schema exists, no data flows
13. **Enforce task state machine** — Hard validation on transitions instead of log-and-allow
14. **Event batching** — Process multiple events per tick with configurable max_events_per_tick
