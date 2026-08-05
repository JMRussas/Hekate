# Tasks API

REST endpoints for managing tasks within orchestration projects -- listing, filtering, updating, retrying, and external execution.

**Base URL:** `http://localhost:5200/api`

All endpoints require JWT authentication via the `Authorization: Bearer <token>` header unless noted otherwise.

---

## List Tasks

List all tasks for a project with filtering and sorting.

```
GET /api/tasks/project/{project_id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Project ID |

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | - | Filter: `pending`, `running`, `completed`, `failed`, `blocked`, `waiting`, `queued`, `cancelled`, `needs_review` |
| `wave` | int | - | Filter by wave number (>= 0) |
| `phase` | string | - | Filter by phase name |
| `model_tier` | string | - | Filter by model tier: `claude_code`, `ollama`, `gemini_cli` |
| `search` | string | - | Substring search across title and description (case-insensitive) |
| `sort` | string | `priority` | Sort field: `priority`, `wave`, `status`, `created_at`, `updated_at` |
| `sort_dir` | string | `asc` | Sort direction: `asc` or `desc` |
| `exclude_output` | bool | false | Omit `output_text` and `output_artifacts` from response (faster for large lists) |
| `limit` | int | 100 | Max results (1-500) |
| `offset` | int | 0 | Pagination offset |

### Response

Returns an array of `TaskOut` objects (see [Task Object](#task-object)).

### Example

```bash
# List all running tasks in wave 1
curl "http://localhost:5200/api/tasks/project/a1b2c3d4e5f6?status=running&wave=1" \
  -H "Authorization: Bearer $TOKEN"
```

```json
[
  {
    "id": "t1a2b3c4d5e6",
    "project_id": "a1b2c3d4e5f6",
    "title": "Implement JWT middleware",
    "status": "running",
    "wave": 1,
    "priority": 1,
    "model_tier": "claude_code",
    "task_type": "code",
    "depends_on": ["t0a0b0c0d0e0"],
    "dependency_details": [
      {"task_id": "t0a0b0c0d0e0", "title": "Set up project structure", "status": "completed"}
    ],
    ...
  }
]
```

---

## Get Task Detail

Get full task detail including output, cost, and verification status.

```
GET /api/tasks/{id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Task ID |

### Example

```bash
curl http://localhost:5200/api/tasks/t1a2b3c4d5e6 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Update Task

Edit a task before execution. Cannot edit running or completed tasks.

```
PATCH /api/tasks/{id}
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | No | Updated title |
| `description` | string | No | Updated description |
| `model_tier` | string | No | Model tier: `claude_code`, `ollama`, `gemini_cli` |
| `priority` | int | No | Priority (lower = higher priority) |
| `max_tokens` | int | No | Token budget for execution |

### Example

```bash
curl -X PATCH http://localhost:5200/api/tasks/t1a2b3c4d5e6 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model_tier": "claude_code", "priority": 1}'
```

---

## Retry Task

Retry a failed or zombie running task. Increments the retry counter.

```
POST /api/tasks/{id}/retry
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `force` | bool | false | Force-retry a running task (zombie recovery). The task must have been running longer than the staleness timeout. |

### Error Responses

| Status | Condition |
|--------|-----------|
| 400 | Task is not failed (and `force` not set) |
| 400 | Max retries reached |
| 409 | Task is running but not stale enough for force-retry |

### Example

```bash
# Retry a failed task
curl -X POST http://localhost:5200/api/tasks/t1a2b3c4d5e6/retry \
  -H "Authorization: Bearer $TOKEN"

# Force-retry a zombie running task
curl -X POST "http://localhost:5200/api/tasks/t1a2b3c4d5e6/retry?force=true" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Cancel Task

Cancel a pending, blocked, waiting, or queued task.

```
POST /api/tasks/{id}/cancel
```

### Example

```bash
curl -X POST http://localhost:5200/api/tasks/t1a2b3c4d5e6/cancel \
  -H "Authorization: Bearer $TOKEN"
```

---

## Bulk Action

Perform an action on multiple tasks at once.

```
POST /api/tasks/bulk
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_ids` | string[] | Yes | Array of task IDs |
| `action` | string | Yes | Action: `retry` or `cancel` |

### Response

```json
{
  "succeeded": ["t1a2b3c4d5e6", "t2b3c4d5e6f7"],
  "failed": [
    {"id": "t3c4d5e6f7g8", "reason": "Not in failed state"}
  ]
}
```

### Example

```bash
curl -X POST http://localhost:5200/api/tasks/bulk \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_ids": ["t1a2b3c4d5e6", "t2b3c4d5e6f7"],
    "action": "retry"
  }'
```

---

## Submit Verification

Submit a verification verdict for a completed task. Internal endpoint called by the Mimir verification agent -- no auth required (task ID is a UUID).

```
POST /api/tasks/{id}/verify
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `verdict` | string | Yes | `passed`, `gaps_found`, or `human_needed` |
| `feedback` | string | No | Explanation (max 500 chars). Required for `gaps_found` and `human_needed`. |

### Verdict Behavior

| Verdict | Effect |
|---------|--------|
| `passed` | Task marked as verified |
| `gaps_found` | Task reset to `pending` with feedback appended to context (up to max retries, then sent to `needs_review`) |
| `human_needed` | Task moved to `needs_review` status |

### Example

```bash
curl -X POST http://localhost:5200/api/tasks/t1a2b3c4d5e6/verify \
  -H "Content-Type: application/json" \
  -d '{"verdict": "gaps_found", "feedback": "Missing error handling for expired tokens"}'
```

---

## Review Task

Respond to a task in `needs_review` status.

```
POST /api/tasks/{id}/review
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | Yes | `approve` or `retry` |
| `feedback` | string | No | Feedback for retry (appended to task context) |

### Example

```bash
curl -X POST http://localhost:5200/api/tasks/t1a2b3c4d5e6/review \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "retry", "feedback": "Use bcrypt instead of SHA-256 for password hashing"}'
```

---

## Expand Epic

Expand an L0 epic task into a new L2 project, inheriting the parent project's repo_path and config.

```
POST /api/tasks/{id}/expand
```

Returns `201` with `{project_id, name}`.

---

## External Executor Endpoints

These endpoints support external task execution (e.g., Claude Code via MCP). The project must use `external` or `hybrid` execution mode.

### List Claimable Tasks

List tasks that are ready for external execution -- `pending` status with all dependencies completed, in the current wave.

```
GET /api/external/{project_id}/claimable
```

### Response

```json
[
  {
    "id": "t1a2b3c4d5e6",
    "title": "Implement JWT middleware",
    "description": "Create Express middleware...",
    "model_tier": "claude_code",
    "wave": 1,
    "priority": 1,
    "phase": "implementation",
    "task_type": "code",
    "depends_on": []
  }
]
```

### Example

```bash
curl http://localhost:5200/api/external/a1b2c3d4e5f6/claimable \
  -H "Authorization: Bearer $TOKEN"
```

---

### Claim Task

Atomically claim a task for external execution. Uses compare-and-swap to prevent race conditions. Returns full task details including context, tools, and system prompt.

```
POST /api/external/tasks/{id}/claim
```

### Response (`TaskClaimResponse`)

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Task ID |
| `project_id` | string | Parent project ID |
| `title` | string | Task title |
| `description` | string | Full task description |
| `task_type` | string | `code`, `research`, `test`, `docs`, `analysis`, `integration` |
| `model_tier` | string | Target model tier |
| `wave` | int | Wave number |
| `priority` | int | Priority (lower = higher) |
| `phase` | string | Execution phase |
| `system_prompt` | string | System prompt for the executor |
| `context` | array | Context entries (dependency outputs, verification feedback, etc.) |
| `tools` | array | Available tools for this task |
| `depends_on` | string[] | Dependency task IDs |
| `rationale` | string | Why this task exists |
| `max_tokens` | int | Token budget |
| `requirement_ids` | string[] | Mapped requirement IDs (R1, R2, etc.) |

### Error Responses

| Status | Condition |
|--------|-----------|
| 404 | Task not found |
| 409 | Task already claimed, not pending, project not executing, or auto execution mode |

### Example

```bash
curl -X POST http://localhost:5200/api/external/tasks/t1a2b3c4d5e6/claim \
  -H "Authorization: Bearer $TOKEN"
```

---

### Submit Result

Submit the output of an externally-executed task. Triggers verification, context forwarding to dependent tasks, and completion handling.

```
POST /api/external/tasks/{id}/result
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `output_text` | string | Yes | Task output/result |
| `model_used` | string | No | Model that produced the output (default: inferred) |
| `prompt_tokens` | int | No | Tokens consumed (input) |
| `completion_tokens` | int | No | Tokens consumed (output) |

### Response (`TaskResultResponse`)

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | Task ID |
| `status` | string | Resulting status (`completed`, `needs_review`, etc.) |
| `verification_status` | string | Verification result if applicable |
| `verification_notes` | string | Verification feedback |
| `next_claimable_task_id` | string | Convenience: next task ready to claim (null if none) |

### Example

```bash
curl -X POST http://localhost:5200/api/external/tasks/t1a2b3c4d5e6/result \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "output_text": "Implemented JWT middleware in src/middleware/auth.ts...",
    "model_used": "claude-code",
    "prompt_tokens": 5000,
    "completion_tokens": 12000
  }'
```

---

### Release Task

Release a claimed task back to pending. Does not increment the retry counter -- use this when you cannot complete a task and want someone else to pick it up.

```
POST /api/external/tasks/{id}/release
```

### Error Responses

| Status | Condition |
|--------|-----------|
| 403 | Task claimed by a different user |
| 409 | Task is not running |

### Example

```bash
curl -X POST http://localhost:5200/api/external/tasks/t1a2b3c4d5e6/release \
  -H "Authorization: Bearer $TOKEN"
```

---

## Task Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Task ID |
| `project_id` | string | Parent project ID |
| `plan_id` | string | Plan that created this task |
| `title` | string | Task title |
| `description` | string | Full description |
| `task_type` | string | `code`, `research`, `test`, `docs`, `analysis`, `integration` |
| `priority` | int | Priority (lower = higher) |
| `status` | string | `pending`, `running`, `completed`, `failed`, `blocked`, `waiting`, `queued`, `cancelled`, `needs_review` |
| `model_tier` | string | `claude_code`, `ollama`, `gemini_cli` |
| `model_used` | string | Actual model that executed the task |
| `tools` | array | Tools available to the executor |
| `prompt_tokens` | int | Input tokens consumed |
| `completion_tokens` | int | Output tokens generated |
| `cost_usd` | float | Estimated cost |
| `output_text` | string | Task output |
| `output_artifacts` | array | Generated file paths |
| `wave` | int | Execution wave (0-based) |
| `phase` | string | Execution phase name |
| `verification_status` | string | `passed`, `gaps_found`, `human_needed`, or null |
| `verification_notes` | string | Verification feedback |
| `requirement_ids` | string[] | Mapped requirement IDs |
| `context` | array | Context entries |
| `error` | string | Error message (if failed) |
| `depends_on` | string[] | Dependency task IDs |
| `dependency_details` | array | `[{task_id, title, status}]` for each dependency |
| `rationale` | string | Why this task was created |
| `started_at` | float | Unix timestamp |
| `completed_at` | float | Unix timestamp |
| `created_at` | float | Unix timestamp |
| `updated_at` | float | Unix timestamp |
| `git_branch` | string | Branch where work was done |
| `git_commit_sha` | string | Commit SHA of the task output |
