# Gods Architecture: Replacing the Monolithic Orchestration Engine

## Context

The current orchestration engine (executor.py ~1120 lines + task_lifecycle.py ~2488 lines) is a monolith that owns planning, dispatch, execution, verification, retries, git, budget, monitoring, and project lifecycle. It has race conditions (duplicate dispatch via set+CAS), leaked state (semaphore replacement, retry backoff dict), false liveness detection (timeout=dead), and tight coupling that means any crash kills everything.

The goal: decompose this into gods — configurable MCP servers with specific roles, communicating through an event relay. Each god is jailed to its responsibilities, independently deployable, independently testable, and replaceable. A god crash does not cascade.

We have working infrastructure: God base class, EventRelay (sinks + gate), demigod runtime (jailed LLM navigation), god_mcp (role-based tool filtering via config), MCPClientManager, registry.

---

## The Gods

| God | Role | Port | Tag | Key Responsibility |
|-----|------|------|-----|-------------------|
| **Odin** | Orchestrator | 5220 | -eye | Dispatch decisions, wave progression, project lifecycle, failure diagnosis |
| **Athena** | Planner | 5221 | -pln | Plan generation (L0-L3), task decomposition, wave reassessment |
| **Hermes** | Worker | 5222 | -wrk | Task execution via CLI, context forwarding, workspace setup |
| **Mimir** | Verifier | 5223 | -tht | Verification, code review, knowledge extraction |
| **Huginn** | Watcher | 5224 | -eye | Real-time anomaly detection, stall/cascade/resource monitoring |
| **Tyche** | Budget | 5225 | -inf | Budget reservation, spend tracking, quota management |
| **Hephaestus** | Git | 5226 | -wrk | Worktrees, branches, PRs, auto-merge |

### Design Principles

- **Odin is the single decision-maker** — eliminates dispatch race conditions
- **Hermes is stateless** — no retry dict, no backoff tracking. Crashes are free.
- **Mimir is the quality gate** — Odin doesn't count a task as done until Mimir says so
- **Huginn only observes** — reports anomalies to Odin, never acts
- **Each god is independently deployable** — can run as NSSM service, subprocess, CLI tool, or MCP server

---

## Event Protocol

All gods communicate through `god_relay_events` table in Postgres. Each god polls for its subscribed event types on its tick interval.

```
project_created        → Athena
project_planned        Athena → Odin
project_started        Odin → Hephaestus, Huginn
dispatch_command       Odin → Tyche (budget gate) → Hermes
worker_event           Hermes → Odin, Mimir, Huginn, Tyche
task_verified          Mimir → Odin
task_rejected          Mimir → Odin
knowledge_extracted    Mimir → (context store)
wave_complete          Odin → Athena, Hephaestus
replan_required        Odin → Athena
pr_created             Hephaestus → Odin
worktree_ready         Hephaestus → Hermes
stall_notification     Huginn → Odin
resource_alert         Huginn → Odin
budget_warning         Tyche → Odin
quota_exceeded         Tyche → Odin
```

---

## Project Flow

```
1. User creates project via REST API
2. API writes project row, relay emits project_created
3. ATHENA: generates plan, decomposes to tasks + DAG, emits project_planned
4. ODIN: transitions project to executing, emits project_started
5. HEPHAESTUS: creates worktree, emits worktree_ready
6. ODIN tick: queries Tyche for budget, selects wave 0 tasks, emits dispatch_commands
7. HERMES: claims tasks, runs CLI executors, emits worker_event:completed
8. MIMIR: verifies output, extracts knowledge, emits task_verified
9. ODIN: counts verified tasks, detects wave complete, emits wave_complete
10. ATHENA: reassesses, decides continue/replan/escalate
11. ODIN: dispatches wave 1 tasks (repeat 6-10)
12. ODIN: all waves done, emits project_complete
13. HEPHAESTUS: creates PR, auto-merges if review passed, destroys worktree
```

---

## Fragility Fixes

| Current Bug | God Fix |
|------------|---------|
| Duplicate dispatch race (set+CAS) | Odin is single decision-maker, serialized tick loop |
| Leaked semaphores on concurrency adjust | No semaphore — Odin tracks count via dispatch_commands + worker acks |
| Stale task detection via timeout | Hermes sends heartbeats, Odin tracks active heartbeats |
| Retry backoff dict never cleaned on crash | Hermes has no retry state. Odin re-dispatches. |
| Branch state confirmed once | Hermes validates worktree on every dispatch |
| Hollow completion from whitespace | Hermes reports raw output. Mimir judges quality. |
| Model discovery cached at startup | Odin queries /providers every tick |
| Identical output false loop detection | Mimir tracks full verification history |
| Migration mtime heuristic | Mimir validates immediately, uses Hermes file report |
| Planning crash kills running tasks | Athena is separate process |

---

## Implementation Phases

1. **Infrastructure** — relay table, PostgresSink, polling mixin
2. **Athena** — planning separates (feature flag: PLANNER_MODE)
3. **Hermes** — execution separates (feature flag: ODIN_DISPATCH)
4. **Mimir** — verification separates (feature flag: VERIFICATION_VIA_MIMIR)
5. **Promote Odin** — absorb executor lifecycle (feature flag: EXECUTOR_MODE)
6. **Remaining gods** — Huginn, Tyche, Hephaestus
7. **Remove monolith** — delete executor.py, task_lifecycle.py, sentinel/

Each phase backward-compatible. Both paths run simultaneously during transition.

---

## God Configs

See `gods/*/god.json` for each god's full configuration including:
- `config.relay.subscriptions` — which events this god listens to
- `config.relay.sinks` — where this god's events go
- `config.features` — toggleable capabilities
- `entry_points` — how to start this god (server, MCP, CLI)
- `tools` — MCP tool interface
- `emits` — events this god produces
