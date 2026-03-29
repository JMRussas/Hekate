# Gods Pipeline — Execution Flow

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Hekate Engine                            │
│                     (FastAPI on port 5200)                      │
│                                                                 │
│  ┌──────────┐    ┌──────────────────────────────────────────┐   │
│  │ REST API │───▶│           Pipeline Event Loop             │   │
│  │          │    │                                          │   │
│  │ /projects│    │  god_relay_events table (Postgres)       │   │
│  │ /tasks   │    │  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐       │   │
│  │ /verify  │    │  │evt 1│→│evt 2│→│evt 3│→│evt 4│→ ...   │   │
│  │ /review  │    │  └─────┘ └─────┘ └─────┘ └─────┘       │   │
│  └──────────┘    │       ↓ cursor-based polling              │   │
│                  │  ┌─────────────────────────────────┐     │   │
│                  │  │     Handler Dispatch             │     │   │
│                  │  │  event_type → [handler1, handler2]│     │   │
│                  │  └─────────────────────────────────┘     │   │
│                  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Event Flow — Happy Path

```
USER creates project via API
         │
         ▼
    ┌─────────┐
    │ project │  event: project_created
    │ created │──────────────────────────────────────────┐
    └─────────┘                                          │
                                                         ▼
                                                 ┌──────────────┐
                                                 │   ATHENA      │
                                                 │  (planner)    │
                                                 │               │
                                                 │ 1. Call LLM   │
                                                 │    Gateway    │
                                                 │    (Claude)   │
                                                 │               │
                                                 │ 2. Parse JSON │
                                                 │    plan       │
                                                 │               │
                                                 │ 3. Validate   │
                                                 │    rules      │
                                                 │               │
                                                 │ 4. Decompose  │
                                                 │    → tasks    │
                                                 └──────┬───────┘
                                                        │
                                              event: project_planned
                                                        │
                              ┌──────────────────────────┤
                              │                          │
                              ▼                          ▼
                      ┌──────────────┐          ┌───────────────┐
                      │    ODIN      │          │CONTEXT BRIDGE │
                      │  (start)     │          │               │
                      │              │          │ Create plan   │
                      │ Set project  │          │ + task nodes  │
                      │ → executing  │          │ in context    │
                      └──────┬───────┘          │ store graph   │
                             │                  └───────────────┘
                   event: project_tick
                             │
                             ▼
                      ┌──────────────┐
                      │    ODIN      │
                      │  (dispatch)  │
                      │              │
                      │ Find pending │
                      │ tasks in     │
                      │ current wave │
                      │              │
                      │ Check slots  │
                      │ Select tier  │
                      └──────┬───────┘
                             │
                    event: dispatch_command (×N tasks)
                             │
                             ▼
                      ┌──────────────┐
                      │   HERMES     │
                      │  (executor)  │
                      │              │
                      │ Spawn Claude │
                      │ Code CLI     │
                      │              │
                      │ async, non-  │
                      │ blocking     │
                      │              │
                      │ max 4 slots  │
                      │ deferred     │
                      │ queue if full│
                      └──────┬───────┘
                             │
                      (background execution 15-30s)
                             │
                   event: worker_event (completed)
                             │
                    ┌────────┴────────┐
                    │                 │
                    ▼                 ▼
             ┌──────────┐     ┌──────────┐
             │  MIMIR   │     │  TYCHE   │
             │(verifier)│     │ (budget) │
             │          │     │          │
             │ Call LLM │     │ Record   │
             │ Gateway  │     │ cost_usd │
             │          │     └──────────┘
             │ Judge:   │
             │ passed?  │
             │ gaps?    │
             │ human?   │
             └────┬─────┘
                  │
                  │ POST /tasks/{id}/verify
                  │ (emits relay event)
                  │
                  ▼
           ┌─────────────┐
           │  /verify     │
           │  endpoint    │
           │              │
           │  Updates DB  │
           │  Emits:      │
           │  task_verified│
           │  (or project_│
           │   tick for   │
           │   retry)     │
           └──────┬───────┘
                  │
                  ▼
           ┌──────────────┐
           │    ODIN      │
           │ (lifecycle)  │
           │              │
           │ Unblock deps │
           │ Check wave   │
           │ completion   │
           │              │
           │ If all done: │
           │ project_     │
           │ complete     │
           │              │
           │ If more:     │
           │ project_tick │
           │ → dispatch   │
           │ next wave    │
           └──────┬───────┘
                  │
         ┌────────┴─────────┐
         │                  │
    (more waves)      (all waves done)
         │                  │
         ▼                  ▼
    Back to ODIN       ┌──────────┐
    dispatch           │ project  │
                       │ complete │
                       └──────┬───┘
                              │
                              ▼
                       ┌───────────────┐
                       │CONTEXT BRIDGE │
                       │               │
                       │ Mark plan     │
                       │ node as       │
                       │ completed     │
                       └───────────────┘
```

## Wave Progression

```
Wave 0: ██████████████ All tasks complete + verified
                                    │
                          odin_lifecycle unblocks wave 1
                                    │
Wave 1: ████████░░░░░░ Executing (3 of 4 done)
         ↑                          │
         │                  hermes deferred queue
         │                  drains as slots open
         │
Wave 2: ░░░░░░░░░░░░░░ Blocked (waiting on wave 1)

█ = completed    ░ = blocked/pending
```

## Services Involved

```
┌─────────────────┐     ┌──────────────────┐     ┌────────────────┐
│  Hekate Engine  │     │   LLM Gateway    │     │ Context Store  │
│  port 5200      │     │   port 5210      │     │ port 5102      │
│                 │     │                  │     │                │
│  Pipeline +     │────▶│  Claude CLI      │     │ Postgres 5433  │
│  REST API       │     │  (planning +     │     │ AGE + pgvector │
│                 │     │   verification)  │     │                │
│  Postgres DB    │     └──────────────────┘     │ Nodes + graph  │
│  (same instance)│                              │ Outbox worker  │
└────────┬────────┘                              └────────────────┘
         │                                              ▲
         │          context_bridge handlers              │
         └──────────────────────────────────────────────┘
```

## Event Types

| Event | Source | Triggers |
|-------|--------|----------|
| `project_created` | API | Athena planning |
| `project_planned` | Athena | Odin start + context bridge |
| `project_tick` | Odin/API | Odin dispatch |
| `dispatch_command` | Odin | Hermes execution |
| `task_running` | Hermes | (informational) |
| `worker_event` | Hermes | Mimir verification + Tyche cost |
| `task_verified` | /verify API | Odin lifecycle |
| `wave_complete` | Odin | Athena reassess |
| `project_complete` | Odin | Context bridge |
| `project_failed` | Odin | (terminal) |
| `needs_human_review` | Mimir | (stalls until /review) |
| `narration` | Athena | (informational) |

## Key Files

| File | Purpose |
|------|---------|
| `gods/engine.py` | DB adapters (Postgres + SQLite), schema, HekateEngine |
| `gods/api.py` | FastAPI routes, /verify and /review endpoints |
| `gods/pipeline.py` | Event loop, cursor, handler dispatch |
| `gods/handlers/registration.py` | Wires all handlers to event types |
| `gods/handlers/athena_leveled.py` | L1 planning via LLM Gateway |
| `gods/handlers/odin.py` | Dispatch, lifecycle, wave progression |
| `gods/handlers/hermes_async.py` | Async CLI execution, deferred queue |
| `gods/handlers/mimir.py` | Verification via gateway + /verify API |
| `gods/handlers/context_bridge.py` | Sync to context store graph |
| `gods/handlers/tyche.py` | Budget tracking |
| `gods/handlers/hephaestus.py` | Git staging |
| `gods/prometheus_mcp.py` | MCP tools for external agents |
| `run_hekate.py` | Entry point, NSSM service config |
