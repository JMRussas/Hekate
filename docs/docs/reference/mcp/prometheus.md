# Prometheus MCP

Project and task management for AI agents. Prometheus is the primary MCP interface to the Hekate orchestration engine -- create projects, monitor execution, manage tasks, and interact with the gods pipeline.

**Port:** 5212
**Transport:** SSE
**Service name:** `HekatePrometheusMcp`

---

## Connection

### Claude Code Configuration

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "prometheus": {
      "url": "http://localhost:5212/sse"
    }
  }
}
```

### Health Check

```bash
curl http://localhost:5212/health
```

```json
{"status": "ok", "service": "prometheus", "port": 5212}
```

---

## Tools

### create_project

Create a new project with requirements. The gods pipeline handles the full lifecycle: planning, dispatch, execution, and verification.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Project name |
| `requirements` | string | Yes | - | Detailed requirements for what needs to be built |
| `repo_path` | string | No | null | Path to the code repository where CLI will execute |
| `target_level` | string | No | `auto` | Planning depth: `auto`, `L1`-`L5` |
| `tdd` | bool | No | true | Enable test-driven development |

**Returns:** Project ID and status.

---

### list_projects

List all projects, optionally filtered by status.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | null | Filter: `draft`, `planning`, `executing`, `completed`, `failed` |

**Returns:** Project list with ID, name, status, and task counts.

---

### project_status

Get detailed project status including all tasks and their current state.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | Yes | The project ID |

**Returns:** Project details with full task breakdown by status, wave, and phase.

---

### list_tasks

List tasks for a project with optional status filtering.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `project_id` | string | Yes | - | The project ID |
| `status` | string | No | null | Filter: `pending`, `running`, `completed`, `failed`, `needs_review`, `blocked` |

**Returns:** Task list with ID, title, status, wave, priority, and model tier.

---

### task_detail

Get full detail for a specific task including description, output, verification status, and cost.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | Yes | The task ID |

**Returns:** Complete task object with output text, error details, and verification notes.

---

### start_project

Start planning and execution for a project. Triggers the gods pipeline: Athena plans, Odin dispatches, Hermes executes, Mimir verifies.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | Yes | The project ID to start |

**Returns:** Confirmation that the pipeline has started.

---

### approve_task

Approve a task in `needs_review` status, accepting its output as-is.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `task_id` | string | Yes | - | The task ID to approve |
| `feedback` | string | No | `""` | Optional feedback to store with the approval |

**Returns:** Updated task status.

---

### retry_task

Retry a failed or `needs_review` task.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | Yes | The task ID to retry |

**Returns:** Confirmation that the task has been reset to pending.

---

### cancel_project

Cancel a project and all its pending/running tasks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | Yes | The project ID to cancel |

**Returns:** Cancellation confirmation.

---

### submit_verification

Submit a verification verdict for a completed task. Used by the Mimir verification agent or any agent performing quality checks.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `task_id` | string | Yes | - | The task ID to verify |
| `verdict` | string | Yes | - | `passed`, `gaps_found`, or `human_needed` |
| `feedback` | string | Yes | - | Explanation of the verdict |
| `confidence` | float | No | 1.0 | Confidence in the verdict (0.0-1.0) |

**Verdict behavior:**

| Verdict | Effect |
|---------|--------|
| `passed` | Task marked as verified, pipeline continues |
| `gaps_found` | Task reset to pending with feedback in context (retries up to max, then `needs_review`) |
| `human_needed` | Task moved to `needs_review` for human judgment |

**Returns:** Acceptance confirmation and resulting action.

---

### submit_plan_children

Submit child plan nodes for a planning node being deepened. Used by Athena sub-agents during recursive planning.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `node_id` | string | Yes | Plan node ID being deepened (from prompt context) |
| `children` | array | Yes | Array of child node objects |

Each child object must contain:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | Yes | Node title |
| `task_type` | string | Yes | `code`, `research`, `test`, `docs` |
| `description` | string | Yes | What this node covers |
| `depends_on_indices` | int[] | No | Sibling dependency indices (0-based) |
| `affected_files` | string[] | No | Files this node touches |
| `complexity` | string | No | Estimated complexity |
| `implementation_notes` | string | No | Implementation guidance |
| `test_strategy` | string | No | Testing approach |

**Returns:** Count of saved nodes and their IDs.

---

### get_events

Get recent pipeline events for a project -- narration, dispatch decisions, verification results, wave transitions, and errors.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `project_id` | string | Yes | - | The project ID |
| `limit` | int | No | 20 | Max events to return |

**Returns:** Array of events with type, timestamp, and payload.

---

## Example Interaction

```
User: Create a project to add rate limiting to the API

Agent calls: create_project(
  name="API Rate Limiting",
  requirements="Add rate limiting to all API endpoints using slowapi...",
  repo_path="C:\\Users\\jruss\\Documents\\GitHub\\MyProject"
)
=> Project ID: a1b2c3d4e5f6

Agent calls: start_project(project_id="a1b2c3d4e5f6")
=> Pipeline started. Athena planning...

Agent calls: project_status(project_id="a1b2c3d4e5f6")
=> Status: executing, 8 tasks (3 completed, 1 running, 4 pending)

Agent calls: get_events(project_id="a1b2c3d4e5f6", limit=5)
=> Recent: task_dispatched, task_completed, verification_passed...
```
