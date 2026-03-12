import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

vi.mock('../api/projects', () => ({
  getProject: vi.fn(),
  listPlans: vi.fn(),
  listTasks: vi.fn(),
  fetchCoverage: vi.fn(),
  fetchCheckpoints: vi.fn(),
  generatePlan: vi.fn(),
  approvePlan: vi.fn(),
  startExecution: vi.fn(),
  pauseExecution: vi.fn(),
  cancelProject: vi.fn(),
  resolveCheckpoint: vi.fn(),
}))
vi.mock('../hooks/useSSE', () => ({
  useSSE: vi.fn(() => ({ events: [], connected: false })),
}))

import {
  getProject, listPlans, listTasks, fetchCoverage, fetchCheckpoints,
  generatePlan, approvePlan, startExecution, pauseExecution, cancelProject,
  resolveCheckpoint,
} from '../api/projects'
import { useSSE } from '../hooks/useSSE'
import ProjectDetail from './ProjectDetail'

const mockGetProject = vi.mocked(getProject)
const mockListPlans = vi.mocked(listPlans)
const mockListTasks = vi.mocked(listTasks)
const mockFetchCoverage = vi.mocked(fetchCoverage)
const mockFetchCheckpoints = vi.mocked(fetchCheckpoints)
const mockGeneratePlan = vi.mocked(generatePlan)
const mockApprovePlan = vi.mocked(approvePlan)
const mockStartExecution = vi.mocked(startExecution)
const mockPauseExecution = vi.mocked(pauseExecution)
const mockCancelProject = vi.mocked(cancelProject)
const mockResolveCheckpoint = vi.mocked(resolveCheckpoint)
const mockUseSSE = vi.mocked(useSSE)

const now = Date.now() / 1000

function makeProject(overrides: Record<string, unknown> = {}) {
  return {
    id: 'proj_001',
    name: 'My Project',
    requirements: 'Build X\nDo Y',
    status: 'draft',
    created_at: now,
    updated_at: now,
    completed_at: null,
    config: {},
    task_summary: null,
    ...overrides,
  }
}

function makePlan(overrides: Record<string, unknown> = {}) {
  return {
    id: 'plan_001',
    project_id: 'proj_001',
    version: 1,
    model_used: 'claude-haiku-4-5-20251001',
    prompt_tokens: 200,
    completion_tokens: 100,
    cost_usd: 0.01,
    plan: { summary: 'Test plan summary', tasks: [] },
    status: 'draft',
    created_at: now,
    ...overrides,
  }
}

function makeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task_001',
    project_id: 'proj_001',
    plan_id: 'plan_001',
    title: 'Task A',
    description: 'Do task A',
    task_type: 'code',
    priority: 50,
    status: 'completed',
    model_tier: 'haiku',
    model_used: 'claude-haiku-4-5-20251001',
    wave: 0,
    tools: [],
    verification_status: null,
    verification_notes: null,
    requirement_ids: [],
    prompt_tokens: 100,
    completion_tokens: 50,
    cost_usd: 0.005,
    output_text: null,
    output_artifacts: [],
    error: null,
    depends_on: [],
    started_at: now,
    completed_at: now,
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

function makeCheckpoint(overrides: Record<string, unknown> = {}) {
  return {
    id: 'cp_001',
    project_id: 'proj_001',
    task_id: 'task_001',
    checkpoint_type: 'retry_exhausted',
    summary: 'Task failed after retries',
    attempts: [],
    question: 'How should we proceed?',
    response: null,
    resolved_at: null,
    created_at: now,
    ...overrides,
  }
}

function setupDefaultMocks(projectOverrides = {}, plans: unknown[] = [], tasks: unknown[] = []) {
  mockGetProject.mockResolvedValue(makeProject(projectOverrides) as Awaited<ReturnType<typeof getProject>>)
  mockListPlans.mockResolvedValue(plans as Awaited<ReturnType<typeof listPlans>>)
  mockListTasks.mockResolvedValue(tasks as Awaited<ReturnType<typeof listTasks>>)
  mockFetchCoverage.mockRejectedValue(new Error('skip'))
  mockFetchCheckpoints.mockResolvedValue([] as Awaited<ReturnType<typeof fetchCheckpoints>>)
  mockUseSSE.mockReturnValue({ events: [], connected: false })
}

function renderProjectDetail() {
  return render(
    <MemoryRouter initialEntries={['/project/proj_001']}>
      <Routes>
        <Route path="/project/:id" element={<ProjectDetail />} />
      </Routes>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('ProjectDetail', () => {
  it('shows loading state', () => {
    mockGetProject.mockReturnValue(new Promise(() => {}))
    mockListPlans.mockReturnValue(new Promise(() => {}))
    mockListTasks.mockReturnValue(new Promise(() => {}))
    mockFetchCoverage.mockReturnValue(new Promise(() => {}))
    mockFetchCheckpoints.mockReturnValue(new Promise(() => {}))
    mockUseSSE.mockReturnValue({ events: [], connected: false })
    renderProjectDetail()
    expect(screen.getByText('Loading project...')).toBeInTheDocument()
  })

  it('shows error when fetch fails', async () => {
    mockGetProject.mockRejectedValue(new Error('Network error'))
    mockListPlans.mockRejectedValue(new Error('skip'))
    mockListTasks.mockRejectedValue(new Error('skip'))
    mockFetchCoverage.mockRejectedValue(new Error('skip'))
    mockFetchCheckpoints.mockRejectedValue(new Error('skip'))
    mockUseSSE.mockReturnValue({ events: [], connected: false })
    renderProjectDetail()
    expect(await screen.findByText(/Error:/)).toBeInTheDocument()
  })

  it('renders project name and status badge', async () => {
    setupDefaultMocks()
    renderProjectDetail()
    expect(await screen.findByText('My Project')).toBeInTheDocument()
    expect(screen.getByText('draft')).toBeInTheDocument()
  })

  it('shows Generate Plan button for draft without plan', async () => {
    setupDefaultMocks({ status: 'draft' }, [])
    renderProjectDetail()
    expect(await screen.findByText('Generate Plan')).toBeInTheDocument()
  })

  it('shows Approve Plan button when draft plan exists', async () => {
    setupDefaultMocks({ status: 'draft' }, [makePlan({ status: 'draft' })])
    renderProjectDetail()
    expect(await screen.findByText('Approve Plan')).toBeInTheDocument()
    // Generate Plan should NOT be visible when a draft plan exists
    expect(screen.queryByText('Generate Plan')).not.toBeInTheDocument()
  })

  it('shows Start Execution for ready status', async () => {
    setupDefaultMocks({ status: 'ready' })
    renderProjectDetail()
    expect(await screen.findByText('Start Execution')).toBeInTheDocument()
  })

  it('shows Pause and Cancel for executing status', async () => {
    setupDefaultMocks({ status: 'executing' })
    renderProjectDetail()
    expect(await screen.findByText('Pause')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('shows Resume for paused status', async () => {
    setupDefaultMocks({ status: 'paused' })
    renderProjectDetail()
    expect(await screen.findByText('Resume')).toBeInTheDocument()
  })

  it('groups tasks by wave with wave headers', async () => {
    setupDefaultMocks({ status: 'completed' }, [], [
      makeTask({ id: 't1', title: 'Wave 0 Task', wave: 0 }),
      makeTask({ id: 't2', title: 'Wave 1 Task', wave: 1, status: 'running' }),
    ])
    renderProjectDetail()
    expect(await screen.findByText('Wave 0 Task')).toBeInTheDocument()
    expect(screen.getByText('Wave 1 Task')).toBeInTheDocument()
    // Multiple waves → wave headers shown (use getAllByText since task titles also match)
    const waveHeaders = screen.getAllByText(/^Wave \d/)
    expect(waveHeaders.length).toBeGreaterThanOrEqual(2)
  })

  it('hides wave headers for single wave', async () => {
    setupDefaultMocks({ status: 'completed' }, [], [
      makeTask({ id: 't1', title: 'Only Task', wave: 0 }),
    ])
    renderProjectDetail()
    expect(await screen.findByText('Only Task')).toBeInTheDocument()
    expect(screen.queryByText(/Wave 0/)).not.toBeInTheDocument()
  })

  it('shows coverage progress bar', async () => {
    setupDefaultMocks()
    mockFetchCoverage.mockResolvedValue({
      project_id: 'proj_001',
      total_requirements: 3,
      covered_count: 2,
      uncovered_count: 1,
      requirements: [
        { id: 'R1', text: 'Build X', covered: true },
        { id: 'R2', text: 'Do Y', covered: true },
        { id: 'R3', text: 'Test Z', covered: false },
      ],
    } as Awaited<ReturnType<typeof fetchCoverage>>)
    renderProjectDetail()
    expect(await screen.findByText('Requirement Coverage')).toBeInTheDocument()
    expect(screen.getByText('2/3')).toBeInTheDocument()
    // Uncovered requirement shown
    expect(screen.getByText(/Test Z/)).toBeInTheDocument()
  })

  it('shows unresolved checkpoints with Resolve button', async () => {
    setupDefaultMocks()
    mockFetchCheckpoints.mockResolvedValue([
      makeCheckpoint(),
    ] as Awaited<ReturnType<typeof fetchCheckpoints>>)
    renderProjectDetail()
    expect(await screen.findByText('Task failed after retries')).toBeInTheDocument()
    expect(screen.getByText('How should we proceed?')).toBeInTheDocument()
    expect(screen.getByText('Resolve')).toBeInTheDocument()
  })

  it('resolve form shows Retry/Skip/Fail buttons', async () => {
    setupDefaultMocks()
    mockFetchCheckpoints.mockResolvedValue([
      makeCheckpoint(),
    ] as Awaited<ReturnType<typeof fetchCheckpoints>>)
    renderProjectDetail()
    await screen.findByText('Task failed after retries')

    const user = userEvent.setup()
    await user.click(screen.getByText('Resolve'))

    expect(screen.getByText('Retry')).toBeInTheDocument()
    expect(screen.getByText('Skip')).toBeInTheDocument()
    expect(screen.getByText('Fail')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Optional guidance...')).toBeInTheDocument()
  })

  it('calls resolveCheckpoint on Retry click', async () => {
    setupDefaultMocks()
    mockFetchCheckpoints.mockResolvedValue([
      makeCheckpoint(),
    ] as Awaited<ReturnType<typeof fetchCheckpoints>>)
    mockResolveCheckpoint.mockResolvedValue(makeCheckpoint({ resolved_at: now }) as Awaited<ReturnType<typeof resolveCheckpoint>>)
    renderProjectDetail()
    await screen.findByText('Task failed after retries')

    const user = userEvent.setup()
    await user.click(screen.getByText('Resolve'))
    await user.type(screen.getByPlaceholderText('Optional guidance...'), 'Try a different approach')
    await user.click(screen.getByText('Retry'))

    await waitFor(() => {
      expect(mockResolveCheckpoint).toHaveBeenCalledWith('cp_001', 'retry', 'Try a different approach')
    })
  })

  it('calls generatePlan when Generate Plan clicked', async () => {
    setupDefaultMocks({ status: 'draft' }, [])
    mockGeneratePlan.mockResolvedValue({ plan_id: 'p1', plan: {}, cost_usd: 0.01 } as Awaited<ReturnType<typeof generatePlan>>)
    renderProjectDetail()
    await screen.findByText('Generate Plan')

    const user = userEvent.setup()
    await user.click(screen.getByText('Generate Plan'))

    await waitFor(() => {
      expect(mockGeneratePlan).toHaveBeenCalledWith('proj_001')
    })
  })

  it('shows plan summary when plan exists', async () => {
    setupDefaultMocks({ status: 'ready' }, [makePlan({ status: 'approved' })])
    renderProjectDetail()
    expect(await screen.findByText('Test plan summary')).toBeInTheDocument()
    expect(screen.getByText('v1')).toBeInTheDocument()
  })

  it('shows SSE events section when connected', async () => {
    setupDefaultMocks({ status: 'executing' })
    mockUseSSE.mockReturnValue({
      events: [
        { type: 'task_start', message: 'Starting task A', project_id: 'proj_001', task_id: 't1', timestamp: now },
      ],
      connected: true,
    })
    renderProjectDetail()
    expect(await screen.findByText('Live Events')).toBeInTheDocument()
    expect(screen.getByText('connected')).toBeInTheDocument()
    expect(screen.getByText('Starting task A')).toBeInTheDocument()
  })
})
