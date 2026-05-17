import { describe, it, expect } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

import { ProjectCanvas } from './ProjectCanvas'
import type { PlanSourceDeps } from './planSource'
import type { Task } from '../types'

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
})
