# The Gods of Hekate

Hekate is the platform — the crossroads where all the gods meet. She is not a service or a tool. She is the system.

## Pipeline Gods

These gods run as handlers inside the Hekate Engine. They process events from the relay table and drive work forward.

### Athena — The Planner

Plans the work. Takes requirements and produces a structured execution plan through progressive levels of detail.

**What she does:**
- Generates L1 plans (rough task breakdown) via LLM
- Deepens to L2 (specs, files, deps), L3 (implementation details), L4 (exact changes), L5 (executable code)
- Rule-checks every level transition before advancing
- Sends the plan for thorough review by a different model
- Incorporates review feedback and regenerates if needed
- Generates TDD test specs when enabled
- Decomposes final plan into executable task rows
- Reassesses after each wave completes

**Talks to:** LLM Gateway (Gemini for generation, different provider for review)

> See [`PLAN_TO_CODE.md`](PLAN_TO_CODE.md) for the architectural model Athena's L1–L5 implements — typed plans as the boundary between AI (planning, body-fill) and machine logic (lowering, emission, validation).

### Odin — The Orchestrator

Decides what runs, when, and on what. Manages the lifecycle of projects from start to completion.

**What he does:**
- Starts projects (planned → executing)
- Dispatches tasks to available providers based on type and complexity
- Manages wave progression (wave 0 completes → unblock wave 1)
- Detects deadlocks (all tasks blocked, none progressing)
- Diagnoses failures (30 error patterns) and decides: retry, reassign provider, skip, or escalate
- Computes retry backoff (fixed, exponential, linear) using Conductor-style task definitions
- Completes projects when all tasks are done

**Talks to:** LLM Gateway (provider availability), Database (task state)

### Hermes — The Executor

Runs the actual work. Launches CLI processes (Claude Code, Gemini CLI) and captures their output.

**What he does:**
- Receives dispatch commands from Odin
- Looks up the CLI provider (Claude Code, Gemini, Codex)
- Builds the prompt with task description, context, and feedback from previous attempts
- Launches the CLI as an async subprocess (non-blocking)
- Captures output, narration events, cost, and token usage
- Reports completion or failure back through the relay
- Manages concurrency (max parallel executions)
- Sends heartbeats for running tasks

**Talks to:** CLI processes (Claude Code, Gemini CLI), Database (task output)

### Mimir — The Verifier

Checks the work. Determines if task output satisfies requirements and extracts knowledge for future use.

**What he does:**
- Runs heuristic quality check (empty output, error patterns, "already done" detection)
- Calls LLM to verify output against task requirements
- Reviews code quality (advisory — stores feedback, never rejects)
- Extracts reusable knowledge from successful completions
- Passes tasks with real output if verification service is unavailable
- Handles all LLM response formats (JSON, markdown-fenced, prose, arrays)

**Talks to:** LLM Gateway (verification, review, knowledge extraction)

### Hephaestus — The Smith

Handles git operations. Stages files after verified tasks.

**What he does:**
- Stages changed files via `git add` after task verification
- Timeout protection on git operations (30s)

**Future:** Commits, creates branches, opens PRs, reverts on failure.

### Tyche — The Accountant

Tracks spending and budget.

**What she does:**
- Records cost from completed tasks (tokens, USD)

**Future:** Budget enforcement, provider quota tracking, cost alerts.

## Infrastructure Gods

These run as standalone services, not inside the pipeline.

### Hades — The Administrator

Manages the underworld — infrastructure, services, deployment. Runs with admin privileges.

**What he does:**
- Lists all NSSM services and their health
- Starts, stops, restarts any service
- Deploys code (sync, build, restart)
- Tails service logs
- Clears pycache
- Executes system commands

**Port:** 5201
**Privilege:** Admin (NSSM service management requires it)

### Prometheus — The Creator

MCP server that lets Claude Code sessions create and manage work. The interface between human intent and the engine.

**What he does:**
- Create projects with requirements and config
- List and query project status
- Start project execution (triggers the pipeline)
- Approve or retry tasks
- Cancel projects
- View events

**Access:** Any Claude Code session with Prometheus in .mcp.json
**Talks to:** Hekate Engine API (HTTP)

### Iris — The Messenger

The dashboard. Shows what's happening across all projects.

**What she does:**
- Displays project list with status
- Shows task breakdown per project
- Streams events (narration, dispatch, verification)
- Budget and service health overview

**Port:** Served by Hekate Engine on 5200

### Apollo — The Oracle (planned)

Code analysis. Understands codebases through static analysis.

**What he does:**
- Full AST analysis (Roslyn for C#, Jedi for Python, TS Compiler for TypeScript)
- Find implementations, usages, patterns
- Contract checking (interfaces, signatures)
- Project dependency graphs
- 32 analysis tools

**Port:** 5110
**Currently named:** `mcp__hekate__` (rename to `mcp__apollo__` planned)

## Designed But Not Built

### Huginn — The Watcher

Rule-based detection. Replaces the old Sentinel monitoring system.

**Intended role:**
- Watch for stuck tasks, stale projects, resource exhaustion
- Apply detection rules without LLM calls
- Trigger interventions when rules fire

### Muninn — The Rememberer

Memory and recall. Huginn's partner.

**Intended role:**
- Track patterns across projects
- Remember what worked and what didn't
- Feed historical context into planning

### Ares — The Guardian

Security review. Pre-plan approval.

**Intended role:**
- Scan plans for security risks (auth, secrets, DB operations)
- Flag dangerous operations before execution
- Review code changes for vulnerabilities

## How They Work Together

```
Human creates project (via Prometheus or Dashboard)
    ↓
Athena plans it (L1 → rule check → L2 → ... → review → TDD → decompose)
    ↓
Odin starts it (set executing, dispatch wave 0)
    ↓
Hermes executes tasks (Claude Code / Gemini CLI)
    ↓
Mimir verifies output (heuristic + LLM)
    ↓
Odin checks wave completion → unblocks next wave
    ↓
Hermes executes wave 1... and so on
    ↓
Odin declares project complete
```

If something fails:
- Mimir rejects → Odin diagnoses → retry with backoff / reassign provider / skip / escalate to human
- Human reviews via Prometheus (approve_task / retry_task)
- Hades restarts services if infrastructure fails

## The Relay

All gods communicate through a single table: `god_relay_events`. Each god emits events and subscribes to events from others. The pipeline processes events in order, advances a cursor, and never replays processed events.

This is the nervous system. Every decision, every state change, every narration is an event in the relay.
