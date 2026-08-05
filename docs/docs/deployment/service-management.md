# Service Management

Hekate services are managed through the Hades admin service, which provides HTTP endpoints and MCP tools for deployment, service control, and monitoring. All operations target the deployment directory at `C:\Hekate`.

## Hades Admin Service

Hades runs on port 5201 as the `HekateAdmin` NSSM service. It is the only service that uses Python 3.14 (all others use 3.11). Hades itself is not managed by Hades -- it must be controlled directly via NSSM.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/deploy` | POST | Full deployment from source |
| `/services` | GET | List all services and their status |
| `/services/{name}/start` | POST | Start a service |
| `/services/{name}/stop` | POST | Stop a service |
| `/services/{name}/restart` | POST | Restart a service |
| `/services/{name}/status` | GET | Get service status |
| `/restart-core` | POST | Restart core services (Engine + Context Store + LLM Gateway) |
| `/restart-all` | POST | Restart all managed services |
| `/logs/{name}` | GET | Tail service logs |
| `/exec` | POST | Execute a shell command |
| `/system-info` | GET | System information |

## Three Ways to Deploy

### 1. Hades MCP Tools (recommended for agents)

MCP tools are available through the Hades MCP bridge on port 5211 (SSE transport):

```
mcp__hades-admin__deploy          # Full deploy
mcp__hades-admin__list_services   # Check all services
mcp__hades-admin__restart_service # Restart a specific service
mcp__hades-admin__restart_core    # Restart core services
mcp__hades-admin__restart_all     # Restart everything
mcp__hades-admin__tail_logs       # View service logs
```

### 2. HTTP API (recommended for scripts)

```bash
# Full deploy
curl -X POST http://localhost:5201/deploy

# List services
curl http://localhost:5201/services

# Restart a specific service
curl -X POST http://localhost:5201/services/HekateOrchestration/restart

# Restart core services
curl -X POST http://localhost:5201/restart-core

# Tail logs
curl "http://localhost:5201/logs/HekateOrchestration?lines=50"
```

### 3. Bash Deploy Script (recommended for manual deploys)

```bash
cd C:\Users\jruss\Documents\GitHub\Hekate
bash scripts/deploy.sh
```

!!! note "Admin terminal required"
    The bash deploy script calls NSSM directly, which requires administrator privileges. The Hades admin service does not have this limitation because it already runs as LocalSystem.

## Deploy Workflow

Whether triggered via MCP, HTTP, or the bash script, the deploy follows the same sequence:

```
 1. Pre-flight checks (source repo exists, tools available)
 2. Stop all managed NSSM services
 3. Create staging directory (C:\Hekate.staging)
 4. dotnet publish Context Store -> staging/context-store/
 5. Copy orchestration source -> staging/orchestration/
 6. Copy Hades source -> staging/hades/
 7. Copy LLM Gateway source -> staging/llm-gateway/
 8. Copy Odin/Gods source -> staging/Odin/
 9. Copy Context Store MCP tools -> staging/context-store/tools/
10. Carry over persistent data (database, config, node_modules)
11. Sync Alembic migrations (source -> staging)
12. Configure Postgres environment for NSSM
13. npm run build orchestration frontend
14. Python syntax check on all .py files
15. Atomic swap: staging -> live (mv C:\Hekate C:\Hekate.old, mv C:\Hekate.staging C:\Hekate)
16. Start all NSSM services
17. Health checks (up to 20 attempts, 1 second apart)
18. If health checks pass: remove .old backup
19. If health checks fail: rollback (swap .old back to live)
```

### Atomic Swap with Rollback

The deploy uses an atomic swap strategy to minimize downtime and enable instant rollback:

```
Before deploy:
  C:\Hekate          <- current live deployment

During deploy:
  C:\Hekate          <- current live (stopped)
  C:\Hekate.staging  <- new build being assembled

After swap:
  C:\Hekate          <- new deployment (was .staging)
  C:\Hekate.old      <- previous deployment (backup)

On failure (rollback):
  C:\Hekate          <- previous deployment restored (was .old)
  C:\Hekate.failed   <- broken deployment saved for investigation
```

!!! warning "Partial deploys"
    Never manually copy files into `C:\Hekate` while services are running. This causes stale code where some files are updated and others are not. Always use the full deploy process.

## Health Check Endpoints

Each service with a health endpoint is checked after deployment:

| Service | Health URL | Expected Response |
|---------|-----------|-------------------|
| HekateOrchestration | `http://localhost:5200/api/health` | JSON with `"ok"` |
| HekateContextStore | `http://localhost:5102/api/health` | JSON with `"ok"` |
| HekateAdmin | `http://localhost:5201/health` | JSON health response |
| HekateLLMGateway | `http://localhost:5210/health` | JSON health response |
| HekateHadesMcp | `http://localhost:5211/health` | JSON health response |
| HekatePrometheusMcp | `http://localhost:5212/health` | JSON health response |
| HekateAgentContextMcp | `http://localhost:5213/health` | JSON health response |
| Ollama | `http://localhost:11434/` | HTTP 200 |
| ComfyUI | `http://localhost:8188/` | HTTP 200 |

Services without health endpoints (HekateServer, worker processes) are checked by port availability only.

The deploy script checks HekateOrchestration and HekateContextStore specifically -- these are the critical-path services. If either fails, the entire deploy is rolled back.

## Log Tailing

### Via Hades

```bash
# HTTP
curl "http://localhost:5201/logs/HekateOrchestration?lines=100"

# MCP
mcp__hades-admin__tail_logs(name="HekateOrchestration", lines=100)
```

### Direct from NSSM log files

If NSSM is configured to write stdout/stderr to log files:

```bash
tail -f /c/Hekate/logs/orchestration.log
```

## Service Groups

Services are organized into groups for batch operations:

| Group | Services | Notes |
|-------|----------|-------|
| **core** | HekateOrchestration, HekateContextStore, HekateLLMGateway | Critical path -- must be running for execution |
| **mcp** | HekateServer, workers, HadesMcp, PrometheusMcp, AgentContextMcp | MCP bridges and code analysis |
| **admin** | HekateAdmin | Self-managing -- not restarted by Hades |
| **external** | Ollama, ComfyUI | Not NSSM-managed, monitored only |

The `restart-core` operation restarts only the core group. The `restart-all` operation restarts all managed services except HekateAdmin (since Hades cannot restart itself).

## Troubleshooting

### Deploy fails at "syntax check"

The deploy runs `compile()` on every `.py` file in the orchestration backend. If any file has a syntax error, the deploy aborts, cleans up the staging directory, and restarts services from the existing (unchanged) deployment.

Check the deploy output for the specific file and line number. Common cause: executor-generated code with raw newlines in string literals instead of `\n`.

### Deploy fails at "health check" -- rollback triggered

The deployment was swapped in but services did not pass health checks within 20 seconds. The deploy automatically rolls back to the previous version.

Check `C:\Hekate.failed` for the broken deployment. Common causes:

- Missing Python dependencies in the deployment directory
- Database migration failure
- Port conflict with another process

### Hades itself is down

Since Hades cannot manage itself, restart it directly:

```bash
nssm restart HekateAdmin
```

Or from an admin PowerShell:

```powershell
Restart-Service HekateAdmin
```

### Services start but immediately crash-loop

NSSM will keep restarting a failed service. Check NSSM's throttle settings:

```bash
nssm get HekateOrchestration AppThrottle
```

If a service is crash-looping, stop it, check logs, fix the issue, then restart:

```bash
nssm stop HekateOrchestration
# ... investigate ...
nssm start HekateOrchestration
```
