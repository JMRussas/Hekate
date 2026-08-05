# Local Development

Minimal setup for running Hekate components from source during development. No NSSM services or deployment scripts required.

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11 | **Not 3.14** -- FastAPI imports break on 3.14 |
| Node.js | 24+ | Required for frontend build and MCP workers |
| .NET SDK | 8+ | Context Store only |
| Docker Desktop | Latest | Only needed if running Context Store |

!!! warning "Python version matters"
    Use `C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe` explicitly if multiple Python versions are installed. Python 3.14 has broken FastAPI imports that cause silent startup failures.

## Quick Start (SQLite Mode)

The fastest path to a working development environment uses SQLite for the orchestration database and skips Docker entirely. This is sufficient for most development work.

```bash
# 1. Install dependencies (one-time)
pip install -r orchestration/requirements.txt
pip install -r Odin/requirements.txt
pip install -r llm-gateway/requirements.txt
pip install -r hades/requirements.txt

# 2. Start the LLM Gateway (required for pipeline)
cd llm-gateway
python server.py
# Runs on port 5210

# 3. Start the Engine (orchestration API + gods pipeline)
cd orchestration
python run.py
# Runs on port 5200
```

With just these two components, you can create projects, generate plans, and execute tasks through the gods pipeline.

## Full Stack Start Order

When running the complete stack, start components in this order:

```
1. Docker Desktop       (if using Context Store)
2. Context Store        (port 5102 -- needs Postgres)
3. LLM Gateway          (port 5210 -- no dependencies)
4. Engine               (port 5200 -- needs LLM Gateway for execution)
5. Hades                (port 5201 -- admin, optional for dev)
```

### Starting each component

**Context Store** (requires Docker):

```bash
# Start Postgres (AGE + pgvector)
cd context-store
docker compose up -d

# Run the .NET API
cd context-store/Api
dotnet run
# Runs on port 5102
```

**LLM Gateway:**

```bash
cd llm-gateway
python server.py
# Runs on port 5210
```

**Orchestration Engine:**

```bash
cd orchestration
python run.py
# Runs on port 5200, serves API + dashboard
```

**Hades Admin** (optional):

```bash
cd hades
python server.py
# Runs on port 5201
```

## SQLite vs Postgres

The orchestration backend supports two database modes:

| Mode | Config | Use Case |
|------|--------|----------|
| **SQLite** (default) | No env var needed | Local dev, single-user, zero setup |
| **Postgres** | Set `ORCHESTRATION_DSN` | Production, multi-connection, full features |

SQLite mode stores the database at `orchestration/data/orchestration.db`. This file is gitignored.

To switch to Postgres:

```bash
export ORCHESTRATION_DSN="postgresql+aiosqlite://postgres:postgres@localhost:5433/orchestration"
cd orchestration
python run.py
```

!!! tip "When to use Postgres"
    SQLite is fine for development. Switch to Postgres when testing concurrent execution, investigating production bugs, or working on database-specific features.

## Debug Mode

The standalone pipeline runner supports a `--debug` flag for verbose logging:

```bash
cd Odin
python run_pipeline.py --debug
```

Additional run_pipeline.py options:

```bash
# Start pipeline and wait for events
python run_pipeline.py

# Create a test project and immediately run it
python run_pipeline.py --create "Build a REST API"

# Inject a project_created event for an existing project
python run_pipeline.py --inject <project-id>
```

## Hot Reload

The orchestration backend (`run.py`) uses uvicorn under the hood. For hot reload during development:

```bash
cd orchestration
python -m uvicorn backend.app:app --reload --port 5200
```

!!! note "Pipeline handlers"
    The gods pipeline handlers (Odin, Athena, Hermes, etc.) are loaded in-process by the engine. Changes to handler code in `Odin/gods/handlers/` require a restart of the engine process.

## Frontend Development

The orchestration dashboard is a React/Vite app:

```bash
cd orchestration/frontend
npm install
npm run dev
# Runs on Vite dev server with HMR
```

The dev server proxies API requests to the backend at port 5200. The production build is served directly by FastAPI from the `dist/` directory.

## Context Store UI

```bash
cd context-store/ui
npm install
npm run dev
# Runs on port 5179
```

## Troubleshooting

### "ModuleNotFoundError" on startup

Ensure you are using Python 3.11, not 3.14. Check with:

```bash
python --version
```

### LLM Gateway not responding

The gateway has a cold start of 60-120 seconds on the first CLI call after restart. This is expected behavior from Claude Code CLI initialization.

### SQLite "database is locked"

This happens when multiple processes access the SQLite database concurrently. In development, ensure only one engine instance is running. For concurrent access, switch to Postgres mode.

### Port already in use

Check what is using the port:

```bash
netstat -ano | findstr :5200
```

Kill the process or change the port via environment variable configuration.
