# Data Flow

This page traces data through the system from project creation to completion.

---

## End-to-End Flow

```mermaid
sequenceDiagram
    participant User
    participant API as Engine API
    participant Relay as god_relay_events
    participant Athena
    participant LLM as LLM Gateway
    participant Odin
    participant Hermes
    participant CLI as Claude Code CLI
    participant Mimir
    participant Heph as Hephaestus
    participant CB as Context Bridge
    participant CS as Context Store

    User->>API: POST /projects
    API->>Relay: project_created

    Relay->>Athena: project_created
    Athena->>LLM: Generate plan (Claude/Gemini)
    LLM-->>Athena: Plan JSON
    Athena->>API: Insert task rows
    Athena->>Relay: project_planned

    Relay->>Odin: project_planned
    Odin->>API: Set project → executing
    Relay->>CB: project_planned
    CB->>CS: Store plan as node tree

    Note over Odin: Periodic ticks drive dispatch
    Odin->>Relay: dispatch_command (×N)

    Relay->>Hermes: dispatch_command
    Hermes->>CLI: Launch subprocess
    CLI-->>Hermes: Output stream + result
    Hermes->>Relay: worker_event (completed)

    par Verification and Budget
        Relay->>Mimir: worker_event
        Mimir->>LLM: Verify output
        LLM-->>Mimir: Verdict
        Mimir->>API: POST /verify
        API->>Relay: task_verified
    and
        Relay->>Hermes: Record cost (Tyche)
    end

    Relay->>Odin: task_verified
    Odin->>API: Unblock dependents
    Relay->>Heph: task_verified
    Heph->>API: git add (staging)
    Relay->>CB: task_verified
    CB->>CS: Store task outcome

    Note over Odin: When all waves complete
    Odin->>Relay: project_complete
    Relay->>CB: project_complete
    CB->>CS: Store project summary
```

---

## Data Stores

### Relay Table (god_relay_events)

The central nervous system. All inter-god communication flows through this table.

```
Direction: Write → Read
Writers: API endpoints, god handlers
Readers: Pipeline event loop (cursor-based)
Retention: Permanent (full audit trail)
```

### Task Database (projects, tasks, plans)

Mutable state for the orchestration layer.

```
Direction: Read/Write
Writers: API endpoints, god handlers (via transition_and_emit)
Readers: API endpoints, dashboard, god handlers
Backend: PostgreSQL (production) or SQLite (development)
```

### Context Store (PostgreSQL + AGE + pgvector)

Persistent agent memory. Write-heavy during execution, read-heavy during planning.

```
Direction: Write (via context bridge), Read (via MCP or API)
Writers: Context bridge handlers, external agents
Readers: Athena (planning context), external agents (semantic search)
Backend: PostgreSQL 16 with AGE and pgvector extensions
```

---

## Communication Protocols

| From | To | Protocol | Purpose |
|------|----|----------|---------|
| Extension | Engine | HTTP REST + SSE | Project management, event streaming |
| Engine | LLM Gateway | HTTP REST + SSE | LLM calls (planning, verification) |
| Engine | Context Store | HTTP REST | Context enrichment, knowledge sync |
| Engine | Hekate MCP | HTTP JSON-RPC | Code analysis for task enrichment |
| Hermes | Claude Code | Subprocess stdin/stdout | Task execution |
| Claude Code | Prometheus MCP | SSE | Project management from within execution |
| Claude Code | Agent Context MCP | SSE | Memory storage/retrieval |
| Hades | NSSM | Process management | Service start/stop/restart |
| Dashboard | Engine | HTTP + SSE | UI rendering + real-time updates |

---

## Event Categories

Events fall into four categories based on their role in the data flow:

### Lifecycle Events

Drive the project forward. Each triggers the next phase.

```
project_created → project_planned → project_started → project_complete
```

### Work Events

Request and report execution.

```
dispatch_command → task_running → worker_event (completed/failed) → task_verified
```

### Infrastructure Events

Wave management and periodic maintenance.

```
tick → project_tick → wave_complete → wave_assessed
```

### Informational Events

Observable but don't drive state changes.

```
narration, budget_spent, files_staged, gate_failed
```
