// Event type → category mapping for color-coded badges

import type { EventCategory } from '../../types'

const EVENT_CATEGORY_MAP: Record<string, EventCategory> = {
  // Planning
  project_created: 'planning',
  project_planned: 'planning',
  plan_generated: 'planning',
  planning_step: 'planning',
  planning_failed: 'planning',

  // Dispatch
  dispatch_command: 'dispatch',
  provider_selected: 'dispatch',
  project_tick: 'dispatch',
  project_started: 'dispatch',

  // Execution
  task_running: 'execution',
  worker_event: 'execution',
  heartbeat: 'execution',
  narration: 'execution',
  cli_prompt: 'execution',
  task_already_running: 'execution',

  // Verification
  task_verified: 'verification',
  verification_started: 'verification',
  verification_deferred: 'verification',
  task_rejected: 'verification',
  needs_human_review: 'verification',
  review_passed: 'verification',
  task_reset: 'verification',

  // Gate
  gate_passed: 'gate',
  gate_failed: 'gate',
  gate_exhausted: 'gate',
  handler_timing: 'gate',

  // Lifecycle
  task_unblocked: 'lifecycle',
  wave_complete: 'lifecycle',
  project_complete: 'lifecycle',
  task_diagnosis: 'lifecycle',
  task_skipped: 'lifecycle',
  task_fork_requested: 'lifecycle',
  files_staged: 'lifecycle',
  project_committed: 'lifecycle',

  // Error
  handler_error: 'error',
  project_failed: 'error',
  hermes_error: 'error',
  odin_error: 'error',
  mimir_error: 'error',
  stage_failed: 'error',

  // Budget
  budget_spent: 'budget',
  rate_limit_hit: 'budget',
}

export function getEventCategory(eventType: string): EventCategory {
  return EVENT_CATEGORY_MAP[eventType] || 'lifecycle'
}

export function getEventSummary(eventType: string, payload: Record<string, unknown>): string {
  switch (eventType) {
    case 'handler_timing':
      return `${payload.handler} ran in ${payload.duration_ms}ms (${payload.emit_count} emit${payload.emit_count === 1 ? '' : 's'})`
    case 'gate_passed':
      return `${payload.handler} gate passed${payload.reason ? `: ${payload.reason}` : ''}`
    case 'gate_failed':
      return `${payload.handler} gate FAILED (${payload.attempt}/${payload.max_attempts}): ${payload.reason}`
    case 'gate_exhausted':
      return `${payload.handler} gate exhausted: ${payload.last_reason}`
    case 'dispatch_command':
      return `Task ${(payload.task_id as string)?.slice(0, 8)} → ${payload.provider}`
    case 'provider_selected':
      return `${payload.provider} selected for ${(payload.task_id as string)?.slice(0, 8)}: ${payload.reason}`
    case 'task_running':
      return `Task ${(payload.task_id as string)?.slice(0, 8)} running via ${payload.provider}`
    case 'worker_event': {
      const status = payload.status as string
      const tid = (payload.task_id as string)?.slice(0, 8)
      if (status === 'completed') {
        return `Task ${tid} completed ($${(payload.cost_usd as number)?.toFixed(4) || '0'})`
      }
      if (payload.timeout) {
        return `Task ${tid} TIMED OUT after ${payload.elapsed_seconds}s`
      }
      return `Task ${tid} ${status}: ${(payload.error as string)?.slice(0, 80) || ''}`
    }
    case 'heartbeat':
      return `Task ${(payload.task_id as string)?.slice(0, 8)} alive (${payload.uptime_s}s)`
    case 'cli_prompt':
      return `Prompt for ${(payload.task_id as string)?.slice(0, 8)} (${payload.prompt_length} chars)`
    case 'narration': {
      const ntype = payload.type as string
      if (ntype === 'tool_use') return `Tool: ${payload.tool}`
      if (ntype === 'assistant') return (payload.text as string)?.slice(0, 100) || 'thinking...'
      return ntype || 'narration'
    }
    case 'task_verified':
      return `Task ${(payload.task_id as string)?.slice(0, 8)} verified (confidence: ${payload.confidence})`
    case 'task_rejected':
      return `Task ${(payload.task_id as string)?.slice(0, 8)} rejected: ${(payload.feedback as string)?.slice(0, 80)}`
    case 'task_unblocked':
      return `${payload.title} (wave ${payload.wave}) unblocked`
    case 'task_diagnosis':
      return `${(payload.task_id as string)?.slice(0, 8)}: ${payload.fix_type} (${(payload.confidence as number)?.toFixed(1)} confidence) — ${payload.root_cause}`
    case 'project_created':
      return `Project created`
    case 'project_planned':
      return `Planning complete (${payload.level})`
    case 'project_complete':
      return `Project completed`
    case 'project_failed':
      return `Project failed: ${payload.reason}`
    case 'wave_complete':
      return `Wave completed`
    case 'budget_spent':
      return `$${(payload.cost_usd as number)?.toFixed(4)} spent`
    case 'handler_error':
      return `${payload.handler} error: ${(payload.error as string)?.slice(0, 80)}`
    default:
      return JSON.stringify(payload).slice(0, 100)
  }
}
