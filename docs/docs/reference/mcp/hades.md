# Hades Admin MCP

Infrastructure administration and service management. Hades provides control over all NSSM services, deployment, command execution, and system diagnostics.

**Port:** 5211
**Transport:** SSE
**Service name:** `HekateHadesMcp`
**Backing API:** Hades HTTP server on port 5201

---

## Connection

### Claude Code Configuration

```json
{
  "mcpServers": {
    "hades": {
      "url": "http://localhost:5211/sse"
    }
  }
}
```

### Health Check

```bash
curl http://localhost:5211/health
```

```json
{"status": "ok", "service": "hades", "port": 5211}
```

---

## Tools

### Service Management

#### list_services

List all Hekate NSSM services with their status and health.

*No parameters.*

**Returns:** JSON array of services with name, status (running/stopped/paused), port, health check result, and group.

---

#### service_status

Get status and health of a specific NSSM service.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Service name (e.g., `HekateEngine`, `HekateContextStore`, `HekateServer`, `Ollama`) |

**Returns:** Service status, PID, uptime, port, and health check result.

---

#### restart_service

Restart a specific NSSM service with health check.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Service name to restart |

**Returns:** Restart result with new PID and health status.

---

#### start_service

Start a specific NSSM service with health check.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Service name to start |

**Returns:** Start result with PID and health status.

---

#### stop_service

Stop a specific NSSM service.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Service name to stop |

**Returns:** Stop confirmation.

---

#### restart_core

Restart core services (Engine + Context Store) with health checks.

*No parameters.*

**Returns:** Status of both services after restart.

---

#### restart_all

Restart all managed Hekate services (those with `managed=true` in the service config).

*No parameters.*

**Returns:** Status of all restarted services.

---

#### create_service

Provision a new NSSM service and register it in the Hades config.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Service name (must start with `Hekate`) |
| `app` | string | Yes | - | Path to the executable |
| `app_args` | string | No | `""` | Command-line arguments |
| `app_dir` | string | No | exe parent dir | Working directory |
| `port` | int | No | 0 | Port the service listens on (0 = none) |
| `health` | string | No | `""` | Health check URL |
| `group` | string | No | `custom` | Service group: `core`, `mcp`, `custom`, `external` |
| `managed` | bool | No | true | Whether deploy should manage this service |

**Returns:** Creation confirmation with NSSM registration details.

---

#### remove_service

Remove an NSSM service and unregister it from the config. Stops the service first if running.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Service name to remove |

**Returns:** Removal confirmation.

---

#### sync_check

Compare the service config against actual NSSM state. Reports drift between expected and actual service registrations.

*No parameters.*

**Returns:** List of discrepancies (missing services, unexpected services, config mismatches).

---

### Command Execution

#### exec

Execute a shell command on the Hekate host machine. Supports bash, cmd, and powershell.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `command` | string | Yes | - | Command to execute (e.g., `npm run build`, `git status`) |
| `cwd` | string | No | source repo root | Working directory |
| `timeout` | int | No | 120 | Max seconds to wait (1-600) |
| `shell` | string | No | `bash` | Shell: `bash`, `cmd`, or `powershell` |

**Returns:** stdout, stderr, and exit code.

---

### Deployment

#### deploy

Run the full deploy script from the source repo to `C:\Hekate`. Stops services, publishes binaries, syncs DB, builds frontend, runs syntax checks, starts services, and performs health checks.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `skip_frontend` | bool | No | false | Skip the frontend npm build step |

**Returns:** Deploy log with step-by-step results and final health status.

---

### Logs

#### tail_logs

Tail the stdout/stderr log files for an NSSM service.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `service` | string | Yes | - | Service name (e.g., `HekateEngine`) |
| `lines` | int | No | 50 | Number of lines from the end |

**Returns:** Recent log output (stdout and stderr).

---

### System

#### system_info

Get basic system information: hostname, platform, Python version, service paths.

*No parameters.*

**Returns:** System details.

---

#### clear_pycache

Recursively delete `__pycache__` directories.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `path` | string | No | `C:\Hekate\orchestration` | Root path to clean |

**Returns:** Count of deleted directories.
