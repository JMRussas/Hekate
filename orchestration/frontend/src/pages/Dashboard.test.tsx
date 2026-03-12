import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

vi.mock('../api/projects', () => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
}))
vi.mock('../api/usage', () => ({
  getBudget: vi.fn(),
}))
vi.mock('../api/services', () => ({
  listServices: vi.fn(),
}))

import { listProjects, createProject } from '../api/projects'
import { getBudget } from '../api/usage'
import { listServices } from '../api/services'
import Dashboard from './Dashboard'

const mockListProjects = vi.mocked(listProjects)
const mockCreateProject = vi.mocked(createProject)
const mockGetBudget = vi.mocked(getBudget)
const mockListServices = vi.mocked(listServices)

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/project/:id" element={<div>Project Page</div>} />
      </Routes>
    </MemoryRouter>
  )
}

function setupDefaultMocks() {
  mockListProjects.mockResolvedValue([])
  mockGetBudget.mockRejectedValue(new Error('skip'))
  mockListServices.mockRejectedValue(new Error('skip'))
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('Dashboard', () => {
  it('shows loading spinner initially', () => {
    mockListProjects.mockReturnValue(new Promise(() => {}))
    mockGetBudget.mockReturnValue(new Promise(() => {}))
    mockListServices.mockReturnValue(new Promise(() => {}))
    renderDashboard()
    expect(screen.getByText('Loading projects...')).toBeInTheDocument()
    expect(screen.queryByText(/No projects yet/)).not.toBeInTheDocument()
  })

  it('shows no projects message when empty', async () => {
    setupDefaultMocks()
    renderDashboard()
    expect(await screen.findByText(/No projects yet/)).toBeInTheDocument()
  })

  it('renders project list with names', async () => {
    mockListProjects.mockResolvedValue([
      { id: 'p1', name: 'My Project', status: 'draft', requirements: '', created_at: Date.now() / 1000, updated_at: Date.now() / 1000, task_summary: { total: 3, completed: 1, running: 1, failed: 0 } },
    ] as Awaited<ReturnType<typeof listProjects>>)
    mockGetBudget.mockRejectedValue(new Error('skip'))
    mockListServices.mockRejectedValue(new Error('skip'))

    renderDashboard()
    expect(await screen.findByText('My Project')).toBeInTheDocument()
    expect(screen.getByText('draft')).toBeInTheDocument()
  })

  it('shows budget cards when data available', async () => {
    mockListProjects.mockResolvedValue([])
    mockGetBudget.mockResolvedValue({
      daily_spent_usd: 1.0, daily_limit_usd: 5.0, daily_pct: 20,
      monthly_spent_usd: 5.0, monthly_limit_usd: 50.0, monthly_pct: 10,
      per_project_limit_usd: 10.0,
    } as Awaited<ReturnType<typeof getBudget>>)
    mockListServices.mockRejectedValue(new Error('skip'))

    renderDashboard()
    expect(await screen.findByText('Daily Budget')).toBeInTheDocument()
    expect(screen.getByText('Monthly Budget')).toBeInTheDocument()
  })

  it('hides budget cards when null', async () => {
    setupDefaultMocks()
    renderDashboard()

    await waitFor(() => expect(mockListProjects).toHaveBeenCalled())
    expect(screen.queryByText('Daily Budget')).not.toBeInTheDocument()
  })

  it('shows service badges', async () => {
    mockListProjects.mockResolvedValue([])
    mockGetBudget.mockRejectedValue(new Error('skip'))
    mockListServices.mockResolvedValue([
      { id: 's1', name: 'Ollama (local)', status: 'online', category: 'LLM', details: {} },
    ] as Awaited<ReturnType<typeof listServices>>)

    renderDashboard()
    expect(await screen.findByText('Ollama')).toBeInTheDocument()
  })

  it('shows fetch error', async () => {
    mockListProjects.mockRejectedValue(new Error('Network error'))
    mockGetBudget.mockRejectedValue(new Error('skip'))
    mockListServices.mockRejectedValue(new Error('skip'))

    renderDashboard()
    expect(await screen.findByText(/Failed to load data/)).toBeInTheDocument()
  })

  it('new project button toggles form', async () => {
    setupDefaultMocks()
    renderDashboard()

    await waitFor(() => expect(mockListProjects).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByText('+ New Project'))
    expect(screen.getByPlaceholderText('My Project')).toBeInTheDocument()

    await user.click(screen.getByText('Cancel'))
    expect(screen.queryByPlaceholderText('My Project')).not.toBeInTheDocument()
  })

  it('validates empty fields', async () => {
    setupDefaultMocks()
    renderDashboard()

    await waitFor(() => expect(mockListProjects).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByText('+ New Project'))
    await user.click(screen.getByText('Create Project'))

    expect(screen.getByText('Name and requirements are required.')).toBeInTheDocument()
    expect(mockCreateProject).not.toHaveBeenCalled()
  })

  it('create project navigates on success', async () => {
    setupDefaultMocks()
    mockCreateProject.mockResolvedValue({ id: 'new_1' } as Awaited<ReturnType<typeof createProject>>)

    renderDashboard()
    await waitFor(() => expect(mockListProjects).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByText('+ New Project'))
    await user.type(screen.getByPlaceholderText('My Project'), 'Test Project')
    await user.type(screen.getByPlaceholderText('Describe what you want built...'), 'Build something')
    await user.click(screen.getByText('Create Project'))

    await waitFor(() => {
      expect(screen.getByText('Project Page')).toBeInTheDocument()
    })
  })

  it('create project shows error on failure', async () => {
    setupDefaultMocks()
    mockCreateProject.mockRejectedValue(new Error('Server error'))

    renderDashboard()
    await waitFor(() => expect(mockListProjects).toHaveBeenCalled())

    const user = userEvent.setup()
    await user.click(screen.getByText('+ New Project'))
    await user.type(screen.getByPlaceholderText('My Project'), 'Test')
    await user.type(screen.getByPlaceholderText('Describe what you want built...'), 'Build X')
    await user.click(screen.getByText('Create Project'))

    await waitFor(() => {
      expect(screen.getByText(/Server error/)).toBeInTheDocument()
    })
  })
})
