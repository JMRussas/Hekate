# Projects API

REST endpoints for managing orchestration projects -- creating, planning, executing, and monitoring AI-driven development tasks.

**Base URL:** `http://localhost:5200/api`

All endpoints require JWT authentication via the `Authorization: Bearer <token>` header unless noted otherwise. Admin users can access all projects; regular users see only their own.

---

## Create Project

Create a new project with requirements.

```
POST /api/projects
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Project name (1-200 chars) |
| `requirements` | string | Yes | What needs to be built (1-50,000 chars) |
| `config` | object | No | Runtime configuration (execution_mode, etc.) |
| `planning_rigor` | string | No | Planning depth: `L1` (quick), `L2` (standard, default), `L3` (thorough) |
| `repo_path` | string | No | Absolute path to the code repository |
| `git_base_branch` | string | No | Base branch for feature branches (default: main) |

### Response

Returns a `ProjectOut` object (see [Project Object](#project-object)).

### Example

```bash
curl -X POST http://localhost:5200/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "User Authentication System",
    "requirements": "Build JWT-based auth with registration, login, refresh tokens, and API key management.",
    "planning_rigor": "L2",
    "repo_path": "C:\\Users\\jruss\\Documents\\GitHub\\MyProject",
    "config": {"execution_mode": "auto"}
  }'
```

```json
{
  "id": "a1b2c3d4e5f6",
  "name": "User Authentication System",
  "requirements": "Build JWT-based auth with...",
  "status": "draft",
  "created_at": 1711900000.0,
  "updated_at": 1711900000.0,
  "config": {"execution_mode": "auto", "planning_rigor": "L2"},
  "planning_rigor": "L2",
  "repo_path": "C:\\Users\\jruss\\Documents\\GitHub\\MyProject",
  "git_base_branch": null,
  "git_project_branch": null,
  "git_state": {},
  "task_summary": null
}
```

---

## List Projects

List projects with optional filtering. Regular users see only their own projects; admins see all.

```
GET /api/projects
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | - | Filter by status: `draft`, `planning`, `ready`, `executing`, `paused`, `completed`, `failed`, `cancelled` |
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

### Response

Returns an array of `ProjectOut` objects with `task_summary` included.

### Example

```bash
curl http://localhost:5200/api/projects?status=executing \
  -H "Authorization: Bearer $TOKEN"
```

```json
[
  {
    "id": "a1b2c3d4e5f6",
    "name": "User Authentication System",
    "status": "executing",
    "task_summary": {"total": 12, "completed": 5, "running": 2, "failed": 0},
    ...
  }
]
```

---

## Get Project Detail

Retrieve a single project with task summary.

```
GET /api/projects/{id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Project ID |

### Example

```bash
curl http://localhost:5200/api/projects/a1b2c3d4e5f6 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Update Project

Update project metadata. Cannot update projects in `executing` or `completed` state.

```
PATCH /api/projects/{id}
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | No | Updated name (1-200 chars) |
| `requirements` | string | No | Updated requirements (1-50,000 chars) |
| `config` | object | No | Updated config (replaces entire config) |
| `planning_rigor` | string | No | Updated rigor level |
| `repo_path` | string | No | Updated repository path |
| `git_base_branch` | string | No | Updated base branch |

### Example

```bash
curl -X PATCH http://localhost:5200/api/projects/a1b2c3d4e5f6 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"planning_rigor": "L3"}'
```

---

## Delete Project

Delete a project and all associated plans, tasks, dependencies, and events (cascade).

```
DELETE /api/projects/{id}
```

Returns `204 No Content` on success.

### Example

```bash
curl -X DELETE http://localhost:5200/api/projects/a1b2c3d4e5f6 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Trigger Planning

Generate an AI execution plan from the project's requirements using Claude via the LLM Gateway. Takes 15-45 seconds depending on complexity and rigor level.

```
POST /api/projects/{id}/plan
```

**Rate limit:** 5 requests per minute.

Cannot plan projects in `executing`, `completed`, or `cancelled` state.

### Response

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | string | Generated plan ID |
| `plan` | object | Structured plan with phases, tasks, and summary |
| `model_used` | string | LLM model used for planning |
| `cost_usd` | float | Cost of the planning call |
| `prompt_tokens` | int | Input tokens consumed |
| `completion_tokens` | int | Output tokens generated |

### Example

```bash
curl -X POST http://localhost:5200/api/projects/a1b2c3d4e5f6/plan \
  -H "Authorization: Bearer $TOKEN"
```

---

## Approve Plan

Approve a draft plan and decompose it into executable tasks with dependency DAG. Runs advisory self-interrogation before decomposing (if enabled).

```
POST /api/projects/{id}/plans/{plan_id}/approve
```

### Response

Returns the decomposition result including created task count, dependency edges, and any interrogation concerns.

### Example

```bash
curl -X POST http://localhost:5200/api/projects/a1b2c3d4e5f6/plans/p1a2b3c4/approve \
  -H "Authorization: Bearer $TOKEN"
```

---

## Review / Deepen Plan

Re-plan with human comments folded in. Optionally change the rigor level to deepen the plan.

```
POST /api/projects/{id}/plans/{plan_id}/review
```

**Rate limit:** 5 requests per minute.

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `target_rigor` | string | No | New rigor level (e.g., `L3` to deepen from `L2`) |

Comments are gathered automatically from the plan's comment thread. Add comments first via `POST /api/projects/{id}/plans/{plan_id}/comments`.

### Example

```bash
curl -X POST http://localhost:5200/api/projects/a1b2c3d4e5f6/plans/p1a2b3c4/review \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_rigor": "L3"}'
```

---

## List Plans

List plan versions for a project, newest first.

```
GET /api/projects/{id}/plans
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | Max results (1-100) |
| `offset` | int | 0 | Pagination offset |

### Example

```bash
curl http://localhost:5200/api/projects/a1b2c3d4e5f6/plans \
  -H "Authorization: Bearer $TOKEN"
```

---

## Git Status

Return live git repository status for the project's `repo_path`.

```
GET /api/projects/{id}/git-status
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `branch` | string | Current branch name |
| `is_dirty` | bool | Whether there are uncommitted changes |
| `modified_files_count` | int | Number of modified files |
| `last_commit` | object | `{sha, message, date}` of the latest commit |
| `open_pr_url` | string | URL of any open PR for this branch (via `gh`) |

Returns `null` if the project has no `repo_path` configured.

### Example

```bash
curl http://localhost:5200/api/projects/a1b2c3d4e5f6/git-status \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "branch": "hekate/user-authentication-system",
  "is_dirty": false,
  "modified_files_count": 0,
  "last_commit": {
    "sha": "c4c99ae",
    "message": "feat: add JWT refresh endpoint",
    "date": "2026-03-30T14:22:00"
  },
  "open_pr_url": "https://github.com/user/repo/pull/42"
}
```

---

## Additional Endpoints

### Start Execution

```
POST /api/projects/{id}/execute
```

Start executing approved tasks. Creates a feature branch (`hekate/{project-slug}`) and sets the project to `executing` status. The gods pipeline picks up tasks on its next tick.

### Pause Execution

```
POST /api/projects/{id}/pause
```

Pause execution -- no new tasks will start. Running tasks continue to completion.

### Cancel Project

```
POST /api/projects/{id}/cancel
```

Cancel the project and all pending/running tasks.

### Clone Project

```
POST /api/projects/{id}/clone
```

Clone a project: copies metadata, latest plan, and all tasks (reset to `pending` status). Returns the new project.

### Export Project

```
GET /api/projects/{id}/export
```

Export full project data as downloadable JSON including plans, tasks, events, checkpoints, usage, and knowledge.

### Requirement Coverage

```
GET /api/projects/{id}/coverage
```

Show which numbered requirements (`[R1]`, `[R2]`, etc.) are covered by at least one task.

### Project Knowledge

```
GET /api/projects/{id}/knowledge
```

List knowledge findings extracted during execution. Filter by `category` query parameter.

```
DELETE /api/projects/{id}/knowledge/{finding_id}
```

Delete a specific knowledge finding.

---

## Project Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | 12-character hex ID |
| `name` | string | Project name |
| `requirements` | string | Full requirements text |
| `status` | string | `draft`, `planning`, `ready`, `executing`, `paused`, `completed`, `failed`, `cancelled` |
| `created_at` | float | Unix timestamp |
| `updated_at` | float | Unix timestamp |
| `completed_at` | float | Unix timestamp (null if not completed) |
| `config` | object | Runtime configuration |
| `planning_rigor` | string | `L1`, `L2`, or `L3` |
| `task_summary` | object | `{total, completed, running, failed}` (included in list/detail) |
| `repo_path` | string | Absolute filesystem path to the repository |
| `git_base_branch` | string | Base branch name |
| `git_project_branch` | string | Feature branch created for this project |
| `git_state` | object | Git state metadata |
