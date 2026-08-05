# Dashboard Tour

Iris is Hekate's dashboard — a React application served by the engine at port 5200. It provides real-time visibility into projects, tasks, events, and system health.

---

## Project List

The home screen shows all projects with:

- **Status badge** — draft, planning, executing, completed, failed
- **Task progress** — completed/total count
- **Cost** — total USD spent
- **Created date**

Click a project to see its details.

---

## Project Detail

### Task Breakdown

Tasks are grouped by wave:

- **Wave 0** tasks appear first (no dependencies)
- **Wave 1** tasks appear next (depend on wave 0)
- Each task shows: status, provider, duration, cost

Task statuses are color-coded:

| Color | Status |
|-------|--------|
| Gray | pending / blocked |
| Blue | running |
| Green | completed |
| Red | failed |
| Yellow | needs_review |

### Event Stream

The right panel streams events in real time via SSE:

- **Narration** — live output from Athena (planning) and Hermes (execution)
- **Dispatch** — task assignment events from Odin
- **Verification** — Mimir's verdicts
- **System** — wave progression, project lifecycle

Events appear as they happen — no polling.

### Task Detail

Click a task to see:

- Full output (truncated to 2000 chars in list, full in detail)
- Execution metadata (provider, model, tokens, cost, duration)
- Verification verdict and feedback
- Retry history (if applicable)

---

## Budget Overview

The budget panel shows:

- **Daily spend** vs daily limit
- **Monthly spend** vs monthly limit
- **Per-project breakdown**
- **Provider cost comparison**

Budget limits are configured in `config.json` under the `budget` section.

---

## Service Health

The health panel shows status for all NSSM services:

| Service | Healthy | Degraded | Down |
|---------|---------|----------|------|
| Hekate Engine | Green | Yellow | Red |
| Context Store | Green | Yellow | Red |
| LLM Gateway | Green | Yellow | Red |
| Hades Admin | Green | Yellow | Red |

Health is determined by HTTP health check endpoints. Services without health endpoints show as "unknown".
