# Quick Start

Get Hekate running and create your first AI-driven project in under 10 minutes.

---

## Prerequisites

| Requirement | Version | Check |
|-------------|---------|-------|
| Python | 3.11 (not 3.14) | `python --version` |
| Node.js | 24+ | `node --version` |
| .NET SDK | 8.0+ | `dotnet --version` |
| Docker | Latest | `docker --version` |
| Git | Any | `git --version` |
| Claude Code | Latest | `claude --version` |

!!! warning "Python 3.14"
    Hekate Engine requires **Python 3.11**. Python 3.14 has broken FastAPI imports. Only the Hades admin service uses 3.14.

---

## 1. Clone and Install

```bash
git clone https://github.com/jrussell-ivern/Hekate.git
cd Hekate

# Install Python dependencies
pip install -r orchestration/requirements.txt
pip install -r Odin/requirements.txt
pip install -r hades/requirements.txt
pip install -r llm-gateway/requirements.txt
```

## 2. Start Infrastructure

```bash
# Start PostgreSQL (with AGE + pgvector)
cd context-store
docker compose up -d
cd ..
```

Verify Postgres is running:

```bash
docker ps  # Should show code-storage-db on port 5433
```

## 3. Configure

Copy the example config and edit it:

```bash
cp orchestration/config.example.json orchestration/config.json
```

Key settings to verify:

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 5200
  },
  "llm_gateway": {
    "gateway_url": "http://localhost:5210"
  },
  "ollama": {
    "hosts": {
      "local": "http://localhost:11434"
    }
  }
}
```

## 4. Start the Engine

```bash
cd Odin
python run_hekate.py
```

You should see:

```
INFO: Hekate Engine starting on port 5200
INFO: Pipeline started — polling relay table
INFO: Registered 12 handlers for 8 event types
```

## 5. Open the Dashboard

Navigate to [http://localhost:5200](http://localhost:5200) in your browser. You should see the Iris dashboard with an empty project list.

## 6. Create Your First Project

=== "curl"

    ```bash
    # Create a project
    curl -X POST http://localhost:5200/api/projects \
      -H "Content-Type: application/json" \
      -d '{
        "name": "Add health endpoint",
        "requirements": "Add a GET /health endpoint to the FastAPI app that returns {\"status\": \"ok\", \"uptime\": seconds}. Include a test.",
        "repo_path": "/path/to/your/project"
      }'

    # Start execution (replace PROJECT_ID from response)
    curl -X POST http://localhost:5200/api/projects/PROJECT_ID/plan
    ```

=== "Prometheus MCP"

    In a Claude Code session with Prometheus configured:

    ```
    Use Prometheus to create a project called "Add health endpoint" with
    requirements: "Add a GET /health endpoint to the FastAPI app that returns
    {status: ok, uptime: seconds}. Include a test."

    Then start the project.
    ```

=== "Dashboard"

    1. Click **New Project** in the dashboard
    2. Enter the name and requirements
    3. Click **Start**

## 7. Watch It Run

The dashboard shows real-time progress:

1. **Athena** generates a plan (you'll see narration events)
2. **Odin** dispatches tasks to Claude Code
3. **Hermes** executes each task (live output streaming)
4. **Mimir** verifies the results
5. **Hephaestus** stages the files

The project completes when all tasks are verified. Check the output in your repo.

---

## What's Next

- [Installation](installation.md) — full setup for all components
- [Your First Project](first-project.md) — detailed walkthrough with explanations
- [Concepts](../concepts/index.md) — understand the mental model
- [Dashboard Tour](dashboard-tour.md) — navigate the Iris UI
