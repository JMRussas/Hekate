# Design Decisions

Key architectural choices and the reasoning behind them.

---

## Why Event-Driven (Not Direct Calls)

**Decision:** Gods communicate exclusively through events in the relay table. No god calls another god directly.

**Alternatives considered:**

- Direct function calls between handlers
- Message queue (RabbitMQ, Redis Streams)
- Actor model (Akka-style)

**Why events won:**

- **Auditability** — every state change is a permanent record. You can reconstruct the full history of any project by querying the relay table.
- **Crash recovery** — if the engine restarts, it reads from the cursor position and resumes. No in-flight state is lost.
- **Decoupling** — adding a new handler (e.g., a security scanner) requires zero changes to existing gods. Just subscribe to `task_verified`.
- **Debugging** — when something goes wrong, the event stream tells you exactly what happened and in what order.

**Trade-off:** Higher latency than direct calls (one DB round-trip per event). Acceptable because task execution (30-300 seconds) dominates total time.

---

## Why Cursor-Based (Not Pub/Sub)

**Decision:** The pipeline polls the relay table using a cursor (last processed event ID), not a pub/sub mechanism.

**Alternatives considered:**

- PostgreSQL LISTEN/NOTIFY
- Redis pub/sub
- In-memory event bus

**Why cursor-based won:**

- **Durability** — if the engine is down for an hour, events accumulate in the table. When it restarts, it processes everything since the cursor. Pub/sub would lose events during downtime.
- **Replay** — for debugging or recovery, you can reset the cursor and replay events. Pub/sub is fire-and-forget.
- **Simplicity** — one table, one query, one cursor. No broker to manage, no subscription state to track.
- **Ordering** — events are naturally ordered by their primary key. No timestamp-based ordering issues.

**Trade-off:** Polling introduces a tick interval (default 1 second). Events aren't processed instantly. For a system where tasks take 30+ seconds, this is negligible.

---

## Why Gods (Not Monolithic Executor)

**Decision:** Split execution into six single-responsibility handlers (Athena, Odin, Hermes, Mimir, Hephaestus, Tyche) instead of one executor that does everything.

**Previous approach:** A monolithic `executor.py` that planned, dispatched, executed, verified, and staged — all in one service. This worked but became fragile at scale.

**Why split:**

- **Isolation** — a bug in verification doesn't crash execution. A slow planner doesn't block dispatch.
- **Testability** — each god can be tested in isolation with mock events.
- **Observability** — the event stream shows exactly which god did what and when.
- **Evolution** — we can replace Athena's planning algorithm without touching Hermes' execution logic.

**Trade-off:** More handler registrations, more event types, slightly more complex debugging. Worth it for the isolation.

---

## Why Generated Plans (Not Hand-Written Workflows)

**Decision:** Athena generates plans from natural language requirements, rather than requiring users to define workflow DAGs manually.

**How Conductor does it:** Users write workflow definitions in JSON — every task, dependency, and branch is specified explicitly.

**Why Hekate generates:**

- **Lower barrier** — users describe what they want in English, not in a DAG specification language
- **Adaptive** — the same requirements can produce different plans based on the codebase, available tooling, and project history
- **Progressive** — L1 gives a rough plan fast; L5 gives an executable spec. Users choose the depth.

**Trade-off:** Generated plans can be wrong. Hekate mitigates this with rule validation, adversarial review, and human approval gates. The plan is always visible and modifiable before execution.

---

## Why Dual Database (Postgres + SQLite)

**Decision:** The engine supports both PostgreSQL (production) and SQLite (development) via a database adapter abstraction.

**Why:**

- **Development speed** — SQLite requires zero setup. Clone the repo, run the engine, it creates the DB automatically.
- **Production reliability** — PostgreSQL provides JSONB for relay events, concurrent access, and the AGE/pgvector extensions the context store needs.

**Implementation:** `gods/engine.py` provides `PostgresDB` and `SQLiteDB` adapters with the same interface. The engine selects based on `ORCHESTRATION_DSN` environment variable (Postgres URI present → Postgres; absent → SQLite).

---

## Why CLI Providers (Not SDK/API)

**Decision:** Task execution uses CLI tools (Claude Code, Gemini CLI) as subprocesses, not the Anthropic/Google SDKs or APIs directly.

**Why:**

- **Full agent capability** — CLI agents have file access, tool use, MCP integration, multi-turn conversation. SDK calls are single-shot.
- **Subscription-based** — CLI tools use existing subscriptions (Claude Pro, Gemini). No separate API billing.
- **Auth simplicity** — CLI tools handle their own OAuth. The LLM Gateway just needs the user's home directory.

**Trade-off:** CLI subprocesses are harder to control than API calls. Cold start (60-120 seconds), unpredictable output formats, and auth failures under NSSM services. The provider abstraction (`gods/providers/`) and error classification system manage this complexity.

---

## Why Single Process (Not Microservices)

**Decision:** All six pipeline gods run in one Python process (the Hekate Engine), not as separate microservices.

**Why:**

- **Operational simplicity** — one process to deploy, monitor, and restart
- **Shared database connection** — no distributed transaction complexity
- **Low latency** — in-process handler dispatch, no network hops between gods
- **Sufficient for scale** — the bottleneck is CLI execution time (30-300 seconds), not handler dispatch

**Future:** The relay table design supports multi-process. If needed, handlers can run in separate processes polling the same table with different cursors. The architecture doesn't prevent it — it's just not needed yet.

---

## Why NSSM (Not Docker for Everything)

**Decision:** Production services run as NSSM Windows services, not Docker containers.

**Why:**

- **GPU access** — Ollama and ComfyUI need direct NVIDIA GPU access. Docker GPU passthrough on Windows is fragile.
- **Filesystem access** — executors need direct access to git repos, worktrees, and the user's home directory for OAuth tokens.
- **Windows-native** — the development machine is Windows. NSSM is the natural choice for background services.

**Trade-off:** No container isolation, manual service management. Hades compensates with automated deploy, health checks, and service control via MCP.
