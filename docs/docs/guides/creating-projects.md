# Creating Projects

Projects are the top-level unit of work in Hekate. A project captures requirements, generates a plan, and executes tasks through the gods pipeline. There are three ways to create one.

---

## Method 1: REST API

Send a `POST` to the Hekate Engine API on port 5200.

### Minimal project

```bash
curl -X POST http://localhost:5200/api/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name": "Add user authentication",
    "requirements": "Implement JWT-based authentication with login, register, and token refresh endpoints. Use bcrypt for password hashing."
  }'
```

**Response:**

```json
{
  "id": "a1b2c3d4e5f6",
  "name": "Add user authentication",
  "requirements": "Implement JWT-based authentication...",
  "status": "draft",
  "created_at": 1711900000.0,
  "updated_at": 1711900000.0,
  "config": {},
  "planning_rigor": "L2"
}
```

### Full configuration

```bash
curl -X POST http://localhost:5200/api/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name": "Inventory system refactor",
    "requirements": "Refactor the inventory module to use an ECS pattern. Support stackable items, equipment slots, and a crafting system.",
    "repo_path": "C:/Repos/my-game",
    "git_base_branch": "main",
    "planning_rigor": "L3",
    "config": {
      "target_level": "L4",
      "tdd": true,
      "review_cycle": true,
      "project_type": "game_dev",
      "platform": "noz"
    }
  }'
```

### Starting execution

Creating a project puts it in `draft` status. To trigger planning and execution:

```bash
curl -X POST http://localhost:5200/api/projects/a1b2c3d4e5f6/plan \
  -H "Authorization: Bearer $TOKEN"
```

---

## Method 2: Prometheus MCP (Claude Code)

If you have the Prometheus MCP server registered (port 5212), you can create projects directly from a Claude Code session using tool calls.

### Create and start

```
Use mcp__prometheus__create_project to create a project:
  name: "Add user authentication"
  requirements: "Implement JWT-based auth with login, register, refresh endpoints."
  repo_path: "C:/Repos/my-app"
  target_level: "auto"
  tdd: true
```

The tool returns a project ID. Then start it:

```
Use mcp__prometheus__start_project with project_id: "a1b2c3d4e5f6"
```

### Available Prometheus tools

| Tool | Purpose |
|------|---------|
| `create_project` | Create a new project |
| `start_project` | Trigger planning and execution |
| `list_projects` | List projects, optionally by status |
| `project_status` | Get project detail with all tasks |
| `list_tasks` | List tasks for a project |
| `task_detail` | Full detail for a single task |
| `approve_task` | Approve a `needs_review` task |
| `retry_task` | Retry a failed task |
| `cancel_project` | Cancel a running project |

---

## Method 3: Dashboard

The orchestration dashboard (served from port 5200) provides a web UI for project management.

1. Navigate to `http://localhost:5200` in your browser
2. Click **New Project**
3. Fill in the project name and requirements
4. Configure options (repo path, planning level, TDD)
5. Click **Create**
6. From the project detail page, click **Plan & Execute** to start

---

## Project Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Project name (1-200 characters) |
| `requirements` | string | required | What needs to be built (1-50,000 characters) |
| `repo_path` | string | `null` | Absolute path to the code repository. This is where CLI executors run. |
| `git_base_branch` | string | `null` | Base branch for worktree creation and PRs |
| `planning_rigor` | enum | `L2` | Planning depth for the orchestration backend: `L0` (roadmap), `L1` (quick), `L2` (standard), `L3` (thorough) |
| `config.target_level` | string | `auto` | Gods pipeline planning depth: `auto`, `L1`-`L5`. See [Plan Levels](plan-levels.md) |
| `config.tdd` | bool | `true` | Enable test-driven development. Tasks include test requirements. |
| `config.review_cycle` | bool | `false` | Enable code review after verification. Max 2 iterations per task. |
| `config.project_type` | string | `default` | `default` or `game_dev`. Game dev enables game-specific task types and platform knowledge. |
| `config.platform` | string | `null` | Game platform: `noz`, `highrise`, `noz_continuing`. Only relevant when `project_type` is `game_dev`. |
| `config.narration` | bool | `true` | Enable narration events for dashboard streaming |
| `config.max_concurrent` | int | `2` | Maximum concurrent task executions |

!!! note "repo_path validation"
    The `repo_path` must be an absolute path and must not contain `..` path components. The engine validates this on creation and rejects relative or traversal paths.

!!! warning "Worktree isolation"
    When `repo_path` is set, the engine creates a git worktree at `.worktrees/{project-slug}/` inside the repo. This isolates the project's branch from the main repo. If worktree creation fails, it falls back to `git checkout` on the main repo -- which can disrupt other work. Always ensure the repo path is valid and accessible.

---

## Project Lifecycle

After creation, a project moves through these statuses:

```
draft → planning → executing → completed
                  ↘ failed
```

| Status | Meaning |
|--------|---------|
| `draft` | Created but not started. Edit requirements here. |
| `planning` | Athena is generating a plan (via LLM Gateway). |
| `executing` | Odin is dispatching tasks. Hermes is running them. |
| `completed` | All tasks finished and verified. PR created if applicable. |
| `failed` | Execution failed after exhausting retries. |

---

## Authentication

The REST API requires a Bearer token. Register a user first:

```bash
# Register
curl -X POST http://localhost:5200/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@local.dev", "password": "your-password"}'

# Login
curl -X POST http://localhost:5200/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@local.dev", "password": "your-password"}'
```

The login response includes an `access_token`. Use it as `Authorization: Bearer <token>` in subsequent requests.

!!! tip "First user is admin"
    The first registered user automatically gets the `admin` role, which grants access to analytics, budget management, and all projects.
