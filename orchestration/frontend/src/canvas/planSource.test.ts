import { describe, it, expect } from 'vitest'
import { Graph } from './graph'
import { PlanSource } from './planSource'
import type { Task } from '../types'

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    project_id: 'p',
    plan_id: 'plan',
    title: 'task one',
    description: '',
    task_type: 'code',
    priority: 0,
    status: 'pending',
    model_tier: 'claude_code',
    model_used: null,
    wave: 0,
    phase: null,
    tools: [],
    context: [],
    verification_status: null,
    verification_notes: null,
    requirement_ids: [],
    prompt_tokens: 0,
    completion_tokens: 0,
    cost_usd: 0,
    output_text: null,
    output_artifacts: [],
    error: null,
    depends_on: [],
    git_branch: null,
    git_commit_sha: null,
    started_at: null,
    completed_at: null,
    created_at: 0,
    ...overrides,
  }
}

describe('PlanSource', () => {
  it('projects each task as a "task" node with its title as intent', async () => {
    const tasks = [
      makeTask({ id: 't1', title: 'read file' }),
      makeTask({ id: 't2', title: 'run tests' }),
    ]
    const src = new PlanSource('proj-1', { listTasks: async () => tasks })
    const g = new Graph()
    await src.load(g)

    expect(g.nodes.size).toBe(2)
    expect(g.nodes.get('t1')?.kind).toBe('task')
    expect(g.nodes.get('t1')?.intent).toBe('read file')
    expect(g.nodes.get('t2')?.intent).toBe('run tests')
  })

  it('maps task status to NodeStatus', async () => {
    const src = new PlanSource('p', {
      listTasks: async () => [
        makeTask({ id: 'a', status: 'pending' }),
        makeTask({ id: 'b', status: 'queued' }),
        makeTask({ id: 'c', status: 'running' }),
        makeTask({ id: 'd', status: 'completed' }),
        makeTask({ id: 'e', status: 'failed' }),
      ],
    })
    const g = new Graph()
    await src.load(g)
    expect(g.nodes.get('a')?.status).toBe('planned')
    expect(g.nodes.get('b')?.status).toBe('ready')
    expect(g.nodes.get('c')?.status).toBe('running')
    expect(g.nodes.get('d')?.status).toBe('done')
    expect(g.nodes.get('e')?.status).toBe('failed')
  })

  it('draws a flow edge for each depends_on relationship', async () => {
    const src = new PlanSource('p', {
      listTasks: async () => [
        makeTask({ id: 'a' }),
        makeTask({ id: 'b', depends_on: ['a'] }),
        makeTask({ id: 'c', depends_on: ['a', 'b'] }),
      ],
    })
    const g = new Graph()
    await src.load(g)
    expect(g.edges).toEqual(
      expect.arrayContaining([
        { fromId: 'a', toId: 'b', fromPort: 'out' },
        { fromId: 'a', toId: 'c', fromPort: 'out' },
        { fromId: 'b', toId: 'c', fromPort: 'out' },
      ]),
    )
    expect(g.edges).toHaveLength(3)
  })
})
