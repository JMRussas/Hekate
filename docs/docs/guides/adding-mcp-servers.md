# Adding MCP Servers

Hekate uses MCP (Model Context Protocol) servers to expose tools to AI agents. This guide covers how to configure existing servers, register them in Claude Code, and create new ones.

---

## `.mcp.json` Configuration

MCP servers are registered via `.mcp.json` files. Claude Code reads this file from the project root to discover available tools.

### Hekate's default configuration

The root `.mcp.json` registers all four MCP servers:

```json
{
  "mcpServers": {
    "agent-context": {
      "type": "sse",
      "url": "http://localhost:5213/sse"
    },
    "hades": {
      "type": "sse",
      "url": "http://localhost:5211/sse"
    },
    "prometheus": {
      "type": "sse",
      "url": "http://localhost:5212/sse"
    },
    "hekate": {
      "type": "http",
      "url": "http://localhost:5110/"
    }
  }
}
```

---

## Transport Types

### SSE (Server-Sent Events)

Used by: Hades MCP (5211), Prometheus MCP (5212), Agent Context MCP (5213)

SSE transport maintains a persistent connection. The server pushes events to the client, and the client sends requests over HTTP POST.

```json
{
  "type": "sse",
  "url": "http://localhost:5213/sse"
}
```

The `/sse` endpoint is the SSE stream. Tool calls are sent to the server's HTTP endpoint alongside the stream.

### HTTP

Used by: Hekate MCP (5110)

HTTP transport uses standard request/response for each tool call. No persistent connection.

```json
{
  "type": "http",
  "url": "http://localhost:5110/"
}
```

### Stdio

Used for local MCP servers that run as subprocesses. Claude Code spawns the process and communicates via stdin/stdout.

```json
{
  "type": "stdio",
  "command": "python",
  "args": ["tools/skills-mcp/server.py"],
  "cwd": "C:/Users/jruss/Documents/GitHub/Hekate/context-store"
}
```

---

## Hekate MCP Servers

### Hades (Admin) -- Port 5211

Infrastructure management. Deploy code, manage NSSM services, execute commands.

| Tool | Purpose |
|------|---------|
| `deploy` | Full deploy from source repo to C:\Hekate |
| `list_services` | Check all NSSM service statuses |
| `restart_service` | Restart a specific service |
| `restart_core` | Restart Engine + Context Store |
| `restart_all` | Restart all services |
| `start_service` / `stop_service` | Start or stop a service |
| `service_status` | Detailed status for one service |
| `create_service` / `remove_service` | Manage NSSM service registrations |
| `exec` | Execute a shell command |
| `sync_check` | Check if source and deployment are in sync |
| `system_info` | System resource usage |
| `tail_logs` | Read recent log output |
| `clear_pycache` | Remove `__pycache__` directories |

### Prometheus (Project Management) -- Port 5212

Project and task lifecycle management. Calls the Hekate Engine API.

| Tool | Purpose |
|------|---------|
| `create_project` | Create a new project |
| `start_project` | Begin planning and execution |
| `list_projects` | List projects with status |
| `project_status` | Project detail with tasks |
| `list_tasks` | Tasks for a project |
| `task_detail` | Full task detail |
| `approve_task` | Approve a needs_review task |
| `retry_task` | Retry a failed task |
| `cancel_project` | Cancel a project |
| `get_events` | Recent pipeline events |
| `submit_plan_children` | Submit plan node children |
| `submit_verification` | Submit verification result |

### Agent Context (Memory) -- Port 5213

Persistent agent memory. See [Context Store Basics](context-store-basics.md) for detailed usage.

| Tool | Purpose |
|------|---------|
| `ensure_project` | Get or create a project |
| `store_node` | Store a knowledge node (auto-embeds) |
| `query_nodes` | Text search across nodes |
| `semantic_search` | Meaning-based search via pgvector |
| `get_node` | Fetch node with attributes |
| `get_children` | List children of a node |
| `list_projects` | List all projects |
| `list_recent` | Recent activity |
| `history` | Temporal edge history |

### Hekate (Code Analysis) -- Port 5110

Roslyn-powered code analysis with multi-language workers.

| Tool | Purpose |
|------|---------|
| `analyze_file` | Deep analysis of a source file |
| `find_usages` | Find all usages of a symbol |
| `find_implementations` | Find interface/class implementations |
| `find_patterns` | Search for code patterns |
| `check_contracts` | Verify interface contracts |
| `check_allocations` | Find heap allocations (perf) |
| `test_impact` | Determine which tests cover changed code |
| `project_graph` | Project dependency graph |
| `build_index` / `index_status` | Build and check code index |
| `plan` / `review_plan` / `deepen_plan` / `finalize_plan` | Plan management |
| `execute` / `verify` / `review` / `iterate` | Execution cycle |

---

## Registering in Claude Code

### Via `.mcp.json` (project-scoped)

Create or edit `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "my-server": {
      "type": "sse",
      "url": "http://localhost:9999/sse"
    }
  }
}
```

Claude Code reads this file on startup. Tools appear as `mcp__my-server__tool_name`.

### Via CLI (global or project-scoped)

```bash
# Add globally
claude mcp add my-server -s user -- python path/to/server.py

# Add to project
claude mcp add my-server -s project -- python path/to/server.py

# Add SSE server
claude mcp add my-server --transport sse --url http://localhost:9999/sse
```

### Verify registration

```bash
claude mcp list
```

---

## How Hermes Passes MCP Config to Executors

When Hermes launches a CLI executor for a task, it passes the project's MCP configuration so the executor has access to the same tools.

The executor (Claude Code subprocess) receives:

1. The `.mcp.json` from the worktree directory (if it exists)
2. Any globally registered MCP servers from the user's Claude Code config
3. The task prompt, which may reference specific MCP tools to use

This means executors can call tools like `mcp__agent-context__store_node` or `mcp__hekate__analyze_file` during task execution, enabling them to store findings and use code analysis tools.

!!! note "Worktree MCP config"
    The `.mcp.json` is copied into the worktree during project setup. If you update the root `.mcp.json`, existing worktrees will not pick up the changes until the next project is created.

---

## Creating Custom MCP Servers

Hekate's MCP servers are built with [FastMCP](https://github.com/jlowin/fastmcp), a Python framework for building MCP servers.

### Minimal example

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-tools", port=9999)


@mcp.tool()
async def greet(name: str) -> str:
    """Greet someone by name.

    Args:
        name: The person to greet
    """
    return f"Hello, {name}!"


@mcp.tool()
async def add_numbers(a: float, b: float) -> str:
    """Add two numbers together.

    Args:
        a: First number
        b: Second number
    """
    return str(a + b)


if __name__ == "__main__":
    mcp.run(transport="sse")
```

### Key patterns from Hekate's servers

**Health endpoint:**

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "my-tools"})
```

**Environment-based configuration:**

```python
import os

PORT = int(os.environ.get("MCP_PORT", "9999"))
DB_HOST = os.environ.get("DB_HOST", "localhost")

mcp = FastMCP("my-tools", port=PORT) if PORT else FastMCP("my-tools")
```

**Docstrings matter:**

Claude Code reads tool docstrings to understand what each tool does. Write clear, specific descriptions with `Args:` sections.

### Running as an NSSM service

To deploy a custom MCP server alongside Hekate's services:

1. Create the server script in the appropriate directory
2. Add dependencies to a `requirements.txt`
3. Register with NSSM:

```bash
nssm install MyMcpServer "C:\path\to\python.exe" "C:\Hekate\my-tools\server.py"
nssm set MyMcpServer AppDirectory "C:\Hekate\my-tools"
nssm set MyMcpServer AppEnvironmentExtra "MCP_PORT=9999"
nssm start MyMcpServer
```

4. Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "my-tools": {
      "type": "sse",
      "url": "http://localhost:9999/sse"
    }
  }
}
```

5. Add to the deploy script (`scripts/deploy.sh`) so it gets copied on deploy.

!!! tip "SSE vs Stdio"
    Use SSE transport for servers that run as persistent services (NSSM). Use Stdio transport for servers that should be spawned on-demand by Claude Code. SSE is preferred for Hekate because the servers are always running and shared across sessions.

---

## Troubleshooting

### "MCP server not responding"

1. Check if the service is running: `curl http://localhost:PORT/health`
2. Check NSSM status: use `mcp__hades__list_services` or `nssm status ServiceName`
3. Check logs: use `mcp__hades__tail_logs` with the service name

### Tools not appearing in Claude Code

1. Verify `.mcp.json` is valid JSON (no trailing commas)
2. Restart Claude Code to re-read the config
3. Check that the server's SSE endpoint is accessible: `curl http://localhost:PORT/sse`

### "Connection refused" on SSE

The MCP server may not be running or may be on a different port. Check:

```bash
netstat -an | grep PORT
```

If the port is not listening, start the service or check for port conflicts.
