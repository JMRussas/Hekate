import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

vi.mock('../api/client', () => ({
  apiFetch: vi.fn(),
}))
vi.mock('../api/projects', () => ({
  reviewTask: vi.fn(),
}))

import { apiFetch } from '../api/client'
import { reviewTask } from '../api/projects'
import TaskDetail from './TaskDetail'

const mockApiFetch = vi.mocked(apiFetch)
const mockReviewTask = vi.mocked(reviewTask)

function makeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task_001',
    project_id: 'proj_001',
    title: 'Build Widget',
    description: 'Create the widget component',
    task_type: 'code',
    status: 'completed',
    model_tier: 'haiku',
    model_used: 'claude-haiku-4-5-20251001',
    cost_usd: 0.005,
    prompt_tokens: 100,
    completion_tokens: 50,
    tools: [],
    depends_on: [],
    wave: 0,
    output_text: null,
    error: null,
    verification_status: null,
    verification_notes: null,
    retry_count: 0,
    max_retries: 5,
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
    ...overrides,
  }
}

function renderTaskDetail() {
  return render(
    <MemoryRouter initialEntries={['/project/proj_001/task/task_001']}>
      <Routes>
        <Route path="/project/:id/task/:taskId" element={<TaskDetail />} />
        <Route path="/project/:id" element={<div>Project Page</div>} />
      </Routes>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('TaskDetail', () => {
  it('shows loading state', () => {
    mockApiFetch.mockReturnValue(new Promise(() => {}))
    renderTaskDetail()
    expect(screen.getByText('Loading task...')).toBeInTheDocument()
  })

  it('shows error state', async () => {
    mockApiFetch.mockRejectedValue(new Error('Not found'))
    renderTaskDetail()
    expect(await screen.findByText(/Error loading task/)).toBeInTheDocument()
  })

  it('renders title and status badge', async () => {
    mockApiFetch.mockResolvedValue(makeTask())
    renderTaskDetail()
    expect(await screen.findByText('Build Widget')).toBeInTheDocument()
    expect(screen.getByText('completed')).toBeInTheDocument()
  })

  it('renders cost and tokens', async () => {
    mockApiFetch.mockResolvedValue(makeTask())
    renderTaskDetail()
    expect(await screen.findByText('$0.0050')).toBeInTheDocument()
    expect(screen.getByText('100 in / 50 out')).toBeInTheDocument()
  })

  it('renders tools badges', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ tools: ['search_knowledge', 'local_llm'] }))
    renderTaskDetail()
    expect(await screen.findByText('search_knowledge')).toBeInTheDocument()
    expect(screen.getByText('local_llm')).toBeInTheDocument()
  })

  it('hides tools section when empty', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ tools: [] }))
    renderTaskDetail()
    await screen.findByText('Build Widget')
    expect(screen.queryByText('Tools')).not.toBeInTheDocument()
  })

  it('shows review panel for needs_review', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ status: 'needs_review' }))
    renderTaskDetail()
    expect(await screen.findByText('Review Required')).toBeInTheDocument()
    expect(screen.getByText('Approve')).toBeInTheDocument()
    expect(screen.getByText('Retry with Feedback')).toBeInTheDocument()
  })

  it('hides review panel for completed', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ status: 'completed' }))
    renderTaskDetail()
    await screen.findByText('Build Widget')
    expect(screen.queryByText('Review Required')).not.toBeInTheDocument()
  })

  it('approve calls reviewTask', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ status: 'needs_review' }))
    mockReviewTask.mockResolvedValue(makeTask({ status: 'completed' }) as Awaited<ReturnType<typeof reviewTask>>)

    renderTaskDetail()
    await screen.findByText('Review Required')

    const user = userEvent.setup()
    await user.click(screen.getByText('Approve'))

    await waitFor(() => {
      expect(mockReviewTask).toHaveBeenCalledWith('task_001', 'approve', '')
    })
  })

  it('retry sends feedback text', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ status: 'needs_review' }))
    mockReviewTask.mockResolvedValue(makeTask() as Awaited<ReturnType<typeof reviewTask>>)

    renderTaskDetail()
    await screen.findByText('Review Required')

    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('Describe what needs to change...'), 'Fix the output')
    await user.click(screen.getByText('Retry with Feedback'))

    await waitFor(() => {
      expect(mockReviewTask).toHaveBeenCalledWith('task_001', 'retry', 'Fix the output')
    })
  })

  it('shows verification card for passed', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ verification_status: 'passed' }))
    renderTaskDetail()
    expect(await screen.findByText('Verification')).toBeInTheDocument()
    expect(screen.getByText('passed')).toBeInTheDocument()
  })

  it('shows verification notes', async () => {
    mockApiFetch.mockResolvedValue(makeTask({
      verification_status: 'gaps_found',
      verification_notes: 'Missing error handling',
    }))
    renderTaskDetail()
    expect(await screen.findByText('Missing error handling')).toBeInTheDocument()
  })

  it('shows error card when error present', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ error: 'Connection failed' }))
    renderTaskDetail()
    expect(await screen.findByText('Connection failed')).toBeInTheDocument()
  })

  it('shows output when present', async () => {
    mockApiFetch.mockResolvedValue(makeTask({ output_text: 'Task result here' }))
    renderTaskDetail()
    expect(await screen.findByText('Task result here')).toBeInTheDocument()
  })
})
