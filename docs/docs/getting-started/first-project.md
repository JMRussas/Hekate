# Your First Project

This walkthrough creates a project, watches it plan and execute, and explains what each god does at every step.

---

## Prerequisites

- Hekate Engine running on port 5200 ([Quick Start](quickstart.md))
- A target repository (any project with code you want to modify)

---

## Step 1: Create the Project

```bash
curl -X POST http://localhost:5200/api/projects \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Add health endpoint",
    "requirements": "Add a GET /health endpoint that returns JSON with status and uptime. Include a unit test. Use the existing app framework.",
    "repo_path": "C:/path/to/your/project"
  }'
```

Response:

```json
{
  "id": "a1b2c3d4-...",
  "name": "Add health endpoint",
  "status": "draft",
  "created_at": 1711900000.0
}
```

The project is in `draft` status. Nothing has happened yet.

---

## Step 2: Start Execution

```bash
curl -X POST http://localhost:5200/api/projects/a1b2c3d4-.../plan
```

This emits a `project_created` event to the relay table. From here, the gods take over.

---

## Step 3: Watch the Pipeline

Open the dashboard at [http://localhost:5200](http://localhost:5200) and click on your project. You'll see events appearing in real time.

### Athena Plans

The first god to act is **Athena**. She:

1. Reads the requirements
2. Calls the LLM Gateway to generate an L1 plan (rough task list)
3. Validates the plan against L1 rules (titles, types, waves present)
4. Deepens to L2 (adds descriptions, files, dependencies, complexity)
5. Sends the plan for adversarial review by a different LLM
6. Incorporates review feedback
7. Decomposes into task rows with a dependency DAG

You'll see narration events like:

```
[Athena] Generating L1 plan for "Add health endpoint"...
[Athena] L1 plan: 3 tasks across 2 waves
[Athena] Deepening to L2...
[Athena] Plan review: confidence 85%, minor suggestion on test coverage
[Athena] Decomposing into tasks...
```

When done, Athena emits `project_planned`. The project moves to `executing`.

### Odin Dispatches

**Odin** receives `project_planned` and:

1. Sets the project status to `executing`
2. Finds wave 0 tasks (no dependencies)
3. Selects a provider for each task (typically `claude_code`)
4. Emits `dispatch_command` for each task

```
[Odin] Project executing — 3 tasks, 2 waves
[Odin] Dispatching wave 0: "Create health endpoint" → claude_code
[Odin] Dispatching wave 0: "Create health test" → claude_code
```

### Hermes Executes

**Hermes** receives each `dispatch_command` and:

1. Transitions the task to `running`
2. Builds a prompt with the task description, context, and any prior feedback
3. Launches Claude Code CLI as an async subprocess
4. Streams output as narration events
5. Captures the final output, cost, and token usage

```
[Hermes] Starting: "Create health endpoint" (claude_code)
[Hermes] Claude Code: Creating /health route in app.py...
[Hermes] Claude Code: Added uptime tracking with time.time()...
[Hermes] Completed: "Create health endpoint" (0.03 USD, 2.1k tokens)
```

### Mimir Verifies

**Mimir** receives the `worker_event` and:

1. Runs a heuristic check (output not empty, no error patterns)
2. Calls a verification LLM to judge the output against requirements
3. Reports: `passed`, `gaps_found`, or `human_needed`

```
[Mimir] Verifying: "Create health endpoint"
[Mimir] Verdict: passed (confidence: 92%)
```

### Odin Progresses

**Odin** receives `task_verified` and:

1. Checks if all wave 0 tasks are verified
2. If yes, emits `wave_complete` and unblocks wave 1
3. Dispatches wave 1 tasks
4. When all waves complete, emits `project_complete`

```
[Odin] Wave 0 complete (2/2 verified)
[Odin] Dispatching wave 1: "Add integration test" → claude_code
...
[Odin] Project complete — 3/3 tasks verified
```

---

## Step 4: Check the Results

The dashboard shows:

- **Project status**: completed
- **Tasks**: all green (completed + verified)
- **Events**: full timeline of every action
- **Cost**: total tokens and USD spent

Check your repository for the changes:

```bash
cd /path/to/your/project
git diff  # See what the agents wrote
```

---

## What If Something Fails?

If a task fails, the pipeline doesn't stop:

1. **Mimir** reports `gaps_found` with specific feedback
2. **Odin** diagnoses the failure (one of 30 error patterns)
3. **Odin** decides: retry with fix instructions, reassign to a different provider, or escalate
4. If retrying, **Hermes** re-executes with the failure feedback injected into the prompt
5. After max retries, the task moves to `needs_review` for human intervention

You can approve or retry tasks via the dashboard, the Prometheus MCP, or the REST API.

---

## Next Steps

- [Plan Levels](../guides/plan-levels.md) — control how deep Athena plans
- [Task Lifecycle](../guides/task-lifecycle.md) — understand state transitions
- [Failure Handling](../guides/failure-handling.md) — diagnosis and recovery patterns
