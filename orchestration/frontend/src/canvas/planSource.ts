// Hekate canvas — PlanSource
//
// Projects an orchestration project's task rows onto a Graph. Each Task
// becomes a `task` node; `depends_on` becomes flow edges; TaskStatus maps
// onto NodeStatus.
//
// Wave/phase containment and plan-level structural nodes (plan root,
// plan_phase, question, risk) come in a later TDD cycle — ship-1 starts
// with the simplest faithful projection: tasks-and-deps.

import type { Task, TaskStatus } from '../types'
import { Graph, type NodeStatus } from './graph'

/**
 * Minimal data source contract — just what PlanSource needs. Tests can
 * pass a stub; production wires through the real API client.
 */
export interface PlanSourceDeps {
  listTasks(projectId: string): Promise<Task[]>
}

// Hekate's TaskStatus → canvas NodeStatus.
// 'blocked' means "waiting on upstream deps" (a future wave), not a
// terminal error — render as 'planned' (gray) so the canvas doesn't
// look like everything failed when a project has just been decomposed.
// 'cancelled' is the same — it's a state, not an error visually.
const STATUS_MAP: Record<TaskStatus, NodeStatus> = {
  pending: 'planned',
  blocked: 'planned',
  queued: 'ready',
  running: 'running',
  completed: 'done',
  needs_review: 'done',
  failed: 'failed',
  cancelled: 'planned',
}

export class PlanSource {
  constructor(
    private readonly projectId: string,
    private readonly deps: PlanSourceDeps,
  ) {}

  async load(graph: Graph): Promise<void> {
    const tasks = await this.deps.listTasks(this.projectId)

    // First pass: add nodes. We need every node present before edges, since
    // Graph.connect rejects unknown ids.
    for (const t of tasks) {
      graph.addNode({
        id: t.id,
        kind: 'task',
        // Stash the full Task so the detail panel can read it without a
        // second API call. The renderer only reads `intent` / `status` etc.;
        // `task` is opaque payload for downstream consumers.
        config: {
          task_type: t.task_type,
          tier: t.model_tier,
          wave: t.wave,
          task: t,
        },
        intent: t.title,
        status: STATUS_MAP[t.status],
      })
    }

    // Second pass: flow edges from depends_on.
    for (const t of tasks) {
      for (const dep of t.depends_on) {
        graph.connect(String(dep), t.id)
      }
    }
  }
}
