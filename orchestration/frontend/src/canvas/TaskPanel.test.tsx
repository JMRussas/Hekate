import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'

import { TaskPanel } from './TaskPanel'
import type { Task } from '../types'

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    project_id: 'p',
    plan_id: 'plan',
    title: 'task one',
    description: 'do the thing',
    task_type: 'code',
    priority: 0,
    status: 'completed',
    model_tier: 'claude_code',
    model_used: null,
    wave: 0,
    phase: null,
    tools: ['Edit', 'Bash'],
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

describe('TaskPanel', () => {
  it('renders the task title, description, and status', () => {
    const task = makeTask({ title: 'Run the tests', description: 'pytest -x', status: 'running' })
    render(<TaskPanel task={task} onClose={() => {}} />)
    expect(screen.getByRole('heading', { name: 'Run the tests' })).toBeInTheDocument()
    expect(screen.getByText('pytest -x')).toBeInTheDocument()
    expect(screen.getByText(/running/i)).toBeInTheDocument()
  })

  it('renders task output when present', () => {
    const task = makeTask({ output_text: 'all 12 tests passed' })
    render(<TaskPanel task={task} onClose={() => {}} />)
    expect(screen.getByText(/all 12 tests passed/)).toBeInTheDocument()
  })

  it('renders the error when present', () => {
    const task = makeTask({ status: 'failed', error: 'AssertionError: bad' })
    render(<TaskPanel task={task} onClose={() => {}} />)
    expect(screen.getByText(/AssertionError: bad/)).toBeInTheDocument()
  })

  it('renders tools used', () => {
    const task = makeTask({ tools: ['Edit', 'Bash', 'Read'] })
    render(<TaskPanel task={task} onClose={() => {}} />)
    expect(screen.getByText('Edit')).toBeInTheDocument()
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('Read')).toBeInTheDocument()
  })

  it('renders tier and wave in the metadata strip', () => {
    const task = makeTask({ model_tier: 'sonnet', wave: 2 })
    render(<TaskPanel task={task} onClose={() => {}} />)
    expect(screen.getByText(/sonnet/i)).toBeInTheDocument()
    expect(screen.getByText(/wave 2/i)).toBeInTheDocument()
  })

  it('fires onClose when the close button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<TaskPanel task={makeTask()} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: /close/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
