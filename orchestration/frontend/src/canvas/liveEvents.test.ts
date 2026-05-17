import { describe, it, expect } from 'vitest'
import { Graph } from './graph'
import { applyLiveEvent } from './liveEvents'
import type { SSEEvent } from '../types'

function event(overrides: Partial<SSEEvent>): SSEEvent {
  return {
    type: 'task_start',
    message: '',
    project_id: 'p',
    task_id: 't1',
    timestamp: 0,
    ...overrides,
  }
}

function seedGraph(): Graph {
  const g = new Graph()
  g.addNode({ id: 't1', kind: 'task', config: {}, intent: 'task 1' })
  return g
}

describe('applyLiveEvent', () => {
  it('task_start sets status to running', () => {
    const g = seedGraph()
    applyLiveEvent(g, event({ type: 'task_start', task_id: 't1' }))
    expect(g.nodes.get('t1')?.status).toBe('running')
  })

  it('task_complete sets status to done and records output if present', () => {
    const g = seedGraph()
    applyLiveEvent(g, event({ type: 'task_complete', task_id: 't1', output: 'ok' }))
    expect(g.nodes.get('t1')?.status).toBe('done')
    expect(g.nodes.get('t1')?.result).toBe('ok')
  })

  it('task_failed sets status to failed and records the error', () => {
    const g = seedGraph()
    applyLiveEvent(g, event({ type: 'task_failed', task_id: 't1', error: 'boom' }))
    expect(g.nodes.get('t1')?.status).toBe('failed')
    expect(g.nodes.get('t1')?.error).toBe('boom')
  })

  it('task_retry resets status to planned', () => {
    const g = seedGraph()
    g.setStatus('t1', 'failed', { error: 'oops' })
    applyLiveEvent(g, event({ type: 'task_retry', task_id: 't1' }))
    expect(g.nodes.get('t1')?.status).toBe('planned')
  })

  it('events for unknown task_id are silently ignored', () => {
    const g = seedGraph()
    expect(() =>
      applyLiveEvent(g, event({ type: 'task_start', task_id: 'nope' })),
    ).not.toThrow()
    expect(g.nodes.get('t1')?.status).toBe('planned')
  })

  it('events without task_id are silently ignored', () => {
    const g = seedGraph()
    expect(() =>
      applyLiveEvent(g, event({ type: 'project_complete', task_id: null })),
    ).not.toThrow()
  })

  it('unknown event types are silently ignored', () => {
    const g = seedGraph()
    applyLiveEvent(g, event({ type: 'tool_call' as never, task_id: 't1' }))
    expect(g.nodes.get('t1')?.status).toBe('planned')
  })
})
