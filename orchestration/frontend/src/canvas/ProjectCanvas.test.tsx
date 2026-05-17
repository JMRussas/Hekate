import { describe, it, expect } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

import { ProjectCanvas, type SubscribeEvents } from './ProjectCanvas'
import type { PlanSourceDeps } from './planSource'
import type { SSEEvent, Task } from '../types'

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    project_id: 'p',
    plan_id: 'plan',
    title: 'task',
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

describe('ProjectCanvas', () => {
  it('shows a loading state initially', () => {
    // never-resolving promise — we just want to assert the pre-load UI
    const pending: PlanSourceDeps = { listTasks: () => new Promise(() => {}) }
    render(<ProjectCanvas projectId="p1" deps={pending} />)
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('renders the loaded canvas with the task labels in the DOM', async () => {
    const deps: PlanSourceDeps = {
      listTasks: async () => [
        makeTask({ id: 't1', title: 'first task' }),
        makeTask({ id: 't2', title: 'second task', depends_on: ['t1'] }),
      ],
    }
    render(<ProjectCanvas projectId="p1" deps={deps} />)

    await waitFor(() =>
      expect(screen.queryByText(/loading/i)).not.toBeInTheDocument(),
    )

    expect(screen.getByText('first task')).toBeInTheDocument()
    expect(screen.getByText('second task')).toBeInTheDocument()
  })

  it('shows an error state when the source fails to load', async () => {
    const failing: PlanSourceDeps = {
      listTasks: async () => {
        throw new Error('network down')
      },
    }
    render(<ProjectCanvas projectId="p1" deps={failing} />)
    await waitFor(() =>
      expect(screen.getByText(/network down/i)).toBeInTheDocument(),
    )
  })

  it('renders each task node with its initial status visible', async () => {
    const deps: PlanSourceDeps = {
      listTasks: async () => [
        makeTask({ id: 't1', title: 'one', status: 'completed' }),
        makeTask({ id: 't2', title: 'two', status: 'running' }),
        makeTask({ id: 't3', title: 'three', status: 'pending' }),
      ],
    }
    render(<ProjectCanvas projectId="p1" deps={deps} />)
    await waitFor(() => expect(screen.getByText('one')).toBeInTheDocument())

    const t1 = screen.getByText('one').closest('[data-testid="canvas-node"]')!
    const t2 = screen.getByText('two').closest('[data-testid="canvas-node"]')!
    const t3 = screen.getByText('three').closest('[data-testid="canvas-node"]')!

    expect(t1).toHaveAttribute('data-status', 'done')
    expect(t2).toHaveAttribute('data-status', 'running')
    expect(t3).toHaveAttribute('data-status', 'planned')
  })

  it('applies SSE events to live-update node status', async () => {
    let emit: ((e: SSEEvent) => void) | null = null
    const subscribeEvents: SubscribeEvents = (_projectId, onEvent) => {
      emit = onEvent
      return () => {
        emit = null
      }
    }

    const deps: PlanSourceDeps = {
      listTasks: async () => [makeTask({ id: 't1', title: 'live task', status: 'pending' })],
    }

    render(
      <ProjectCanvas
        projectId="p1"
        deps={deps}
        subscribeEvents={subscribeEvents}
      />,
    )

    await waitFor(() => expect(screen.getByText('live task')).toBeInTheDocument())
    const card = () =>
      screen.getByText('live task').closest('[data-testid="canvas-node"]')!
    expect(card()).toHaveAttribute('data-status', 'planned')

    act(() => {
      emit?.({
        type: 'task_start',
        message: '',
        project_id: 'p1',
        task_id: 't1',
        timestamp: 0,
      })
    })
    expect(card()).toHaveAttribute('data-status', 'running')

    act(() => {
      emit?.({
        type: 'task_complete',
        message: '',
        project_id: 'p1',
        task_id: 't1',
        timestamp: 0,
        output: 'all done',
      })
    })
    expect(card()).toHaveAttribute('data-status', 'done')
  })

  it('clicking a node opens the TaskPanel with that task', async () => {
    const deps: PlanSourceDeps = {
      listTasks: async () => [
        makeTask({ id: 't1', title: 'first', description: 'do the first thing' }),
        makeTask({ id: 't2', title: 'second', description: 'do the second thing' }),
      ],
    }
    render(<ProjectCanvas projectId="p1" deps={deps} />)
    await waitFor(() => expect(screen.getByText('first')).toBeInTheDocument())
    expect(screen.queryByTestId('task-panel')).not.toBeInTheDocument()

    // fireEvent.click rather than userEvent.click to skip the mousedown/up
    // sequence — React Flow attaches d3-drag handlers to mousedown that fire
    // async and try to read `document` after jsdom has torn down, polluting
    // the test run with "unhandled errors" even though the assertions pass.
    fireEvent.click(screen.getByText('first'))

    const panel = await screen.findByTestId('task-panel')
    expect(panel).toBeInTheDocument()
    expect(panel).toHaveTextContent('first')
    expect(panel).toHaveTextContent('do the first thing')
  })

  it('clicking close on the TaskPanel hides it', async () => {
    const deps: PlanSourceDeps = {
      listTasks: async () => [makeTask({ id: 't1', title: 'first' })],
    }
    render(<ProjectCanvas projectId="p1" deps={deps} />)
    await waitFor(() => expect(screen.getByText('first')).toBeInTheDocument())

    fireEvent.click(screen.getByText('first'))
    await screen.findByTestId('task-panel')

    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    expect(screen.queryByTestId('task-panel')).not.toBeInTheDocument()
  })
})
