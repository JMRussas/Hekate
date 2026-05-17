// Hekate canvas — SSE event → Graph mutation projection.
//
// Maps the orchestration's SSE event stream onto graph status mutations.
// Pure function: takes an event, mutates the graph, returns void. The
// reverse mapping of execution-time intent — task_start→running,
// task_complete→done, task_failed→failed, task_retry→planned — is the
// minimum the canvas needs to feel alive.
//
// Events without a task_id (project_*, budget_warning, wave_checkpoint)
// and events for nodes not present in the graph are silently ignored.
// "Silently" because the stream is opportunistic — if we miss a node
// because the initial load is mid-flight, that's recoverable on the
// next status event, not a fatal condition.

import type { Graph } from './graph'
import type { SSEEvent } from '../types'

export function applyLiveEvent(graph: Graph, ev: SSEEvent): void {
  const taskId = ev.task_id
  if (!taskId) return
  if (!graph.nodes.has(taskId)) return

  switch (ev.type) {
    case 'task_start':
      graph.setStatus(taskId, 'running')
      return
    case 'task_complete': {
      const output = typeof ev.output === 'string' ? ev.output : undefined
      graph.setStatus(taskId, 'done', output !== undefined ? { result: output } : {})
      return
    }
    case 'task_failed': {
      const error = typeof ev.error === 'string' ? ev.error : ev.message || undefined
      graph.setStatus(taskId, 'failed', error !== undefined ? { error } : {})
      return
    }
    case 'task_retry':
    case 'task_verification_retry':
      graph.setStatus(taskId, 'planned')
      return
    case 'task_needs_review':
      // verification surfaced gaps; treat as done for visibility (the
      // existing dashboard surfaces the actual review state in the
      // detail panel — once we ship that, refine this)
      graph.setStatus(taskId, 'done')
      return
    default:
      // tool_call, task_output, task_error etc. — interesting later when
      // we surface trace events as containment children, ignored for now
      return
  }
}
