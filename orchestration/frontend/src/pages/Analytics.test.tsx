// Orchestration Engine - Analytics Page Tests

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../api/analytics', () => ({
  getCostBreakdown: vi.fn(),
  getTaskOutcomes: vi.fn(),
  getEfficiency: vi.fn(),
}))

import { getCostBreakdown, getTaskOutcomes, getEfficiency } from '../api/analytics'
import type { CostBreakdown, TaskOutcomes, Efficiency } from '../api/analytics'
import Analytics from './Analytics'

const mockGetCostBreakdown = vi.mocked(getCostBreakdown)
const mockGetTaskOutcomes = vi.mocked(getTaskOutcomes)
const mockGetEfficiency = vi.mocked(getEfficiency)

const mockCost: CostBreakdown = {
  by_project: [
    { project_id: 'p1', project_name: 'Project Alpha', cost_usd: 0.15, task_count: 5 },
  ],
  by_model_tier: [
    { model_tier: 'haiku', cost_usd: 0.05, task_count: 3, avg_cost_per_task: 0.0167 },
    { model_tier: 'sonnet', cost_usd: 0.10, task_count: 2, avg_cost_per_task: 0.05 },
  ],
  daily_trend: [
    { date: '2025-01-15', cost_usd: 0.15, api_calls: 5 },
  ],
  total_cost_usd: 0.15,
}

const mockOutcomes: TaskOutcomes = {
  by_tier: [
    { model_tier: 'haiku', total: 3, completed: 2, failed: 1, needs_review: 0, success_rate: 0.6667 },
    { model_tier: 'sonnet', total: 2, completed: 2, failed: 0, needs_review: 0, success_rate: 1.0 },
  ],
  verification_by_tier: [
    { model_tier: 'haiku', total_verified: 2, passed: 2, gaps_found: 0, human_needed: 0, pass_rate: 1.0 },
  ],
}

const mockEfficiency: Efficiency = {
  retries_by_tier: [
    { model_tier: 'haiku', total_tasks: 3, tasks_with_retries: 1, total_retries: 2, retry_rate: 0.3333 },
  ],
  checkpoint_count: 2,
  unresolved_checkpoint_count: 1,
  wave_throughput: [
    { project_id: 'p1', project_name: 'Project Alpha', wave: 0, task_count: 3, avg_duration_seconds: 45.5 },
    { project_id: 'p1', project_name: 'Project Alpha', wave: 1, task_count: 2, avg_duration_seconds: 30.0 },
  ],
  cost_efficiency: [
    { model_tier: 'haiku', cost_usd: 0.05, tasks_completed: 2, verification_pass_count: 2, cost_per_pass: 0.025 },
  ],
}

function setupMocks() {
  mockGetCostBreakdown.mockResolvedValue(mockCost)
  mockGetTaskOutcomes.mockResolvedValue(mockOutcomes)
  mockGetEfficiency.mockResolvedValue(mockEfficiency)
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('Analytics', () => {
  it('shows loading state', () => {
    mockGetCostBreakdown.mockReturnValue(new Promise(() => {}))
    mockGetTaskOutcomes.mockReturnValue(new Promise(() => {}))
    mockGetEfficiency.mockReturnValue(new Promise(() => {}))

    render(<Analytics />)
    expect(screen.getByText('Loading analytics...')).toBeInTheDocument()
  })

  it('renders cost breakdown section', async () => {
    setupMocks()
    render(<Analytics />)

    expect(await screen.findByText('Cost Breakdown')).toBeInTheDocument()
    // Project Alpha appears in cost breakdown AND wave throughput tables
    expect(screen.getAllByText('Project Alpha').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Total Task Cost:')).toBeInTheDocument()
  })

  it('renders model tier badges', async () => {
    setupMocks()
    render(<Analytics />)

    await waitFor(() => {
      const badges = document.querySelectorAll('.badge.haiku')
      expect(badges.length).toBeGreaterThan(0)
    })
  })

  it('renders daily trend table', async () => {
    setupMocks()
    render(<Analytics />)

    expect(await screen.findByText('2025-01-15')).toBeInTheDocument()
    expect(screen.getByText('Daily Trend')).toBeInTheDocument()
  })

  it('renders task outcomes section', async () => {
    setupMocks()
    render(<Analytics />)

    expect(await screen.findByText('Task Outcomes')).toBeInTheDocument()
    expect(screen.getByText('Success Rate by Tier')).toBeInTheDocument()
    expect(screen.getByText('Verification Signal')).toBeInTheDocument()
  })

  it('renders efficiency section', async () => {
    setupMocks()
    render(<Analytics />)

    expect(await screen.findByText('Efficiency')).toBeInTheDocument()
    expect(screen.getByText('Retries by Tier')).toBeInTheDocument()
    expect(screen.getByText('Wave Throughput')).toBeInTheDocument()
  })

  it('shows checkpoint counts', async () => {
    setupMocks()
    render(<Analytics />)

    await waitFor(() => {
      expect(screen.getByText(/2 total/)).toBeInTheDocument()
      expect(screen.getByText(/1 unresolved/)).toBeInTheDocument()
    })
  })

  it('wave throughput shows project name', async () => {
    setupMocks()
    render(<Analytics />)

    await waitFor(() => {
      // Project column in wave throughput table
      const cells = screen.getAllByText('Project Alpha')
      expect(cells.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('renders dash for null cost_per_pass', async () => {
    const effWithNull: Efficiency = {
      ...mockEfficiency,
      cost_efficiency: [
        { model_tier: 'haiku', cost_usd: 0.05, tasks_completed: 2, verification_pass_count: 0, cost_per_pass: null },
      ],
    }
    mockGetCostBreakdown.mockResolvedValue(mockCost)
    mockGetTaskOutcomes.mockResolvedValue(mockOutcomes)
    mockGetEfficiency.mockResolvedValue(effWithNull)

    render(<Analytics />)

    await waitFor(() => {
      // cost_per_pass is null → should render '-'
      expect(screen.getByText('-')).toBeInTheDocument()
    })
  })

  it('shows error on all fetch failures', async () => {
    mockGetCostBreakdown.mockRejectedValue(new Error('Network error'))
    mockGetTaskOutcomes.mockRejectedValue(new Error('Network error'))
    mockGetEfficiency.mockRejectedValue(new Error('Network error'))

    render(<Analytics />)
    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
  })

  it('shows partial data and error on partial failure', async () => {
    // Cost succeeds, others fail
    mockGetCostBreakdown.mockResolvedValue(mockCost)
    mockGetTaskOutcomes.mockRejectedValue(new Error('Outcomes failed'))
    mockGetEfficiency.mockRejectedValue(new Error('Efficiency failed'))

    render(<Analytics />)

    // Should show the error banner
    expect(await screen.findByText(/Failed to load/)).toBeInTheDocument()
    // Should still render the cost data that succeeded
    expect(screen.getByText('Cost Breakdown')).toBeInTheDocument()
    expect(screen.getByText('Project Alpha')).toBeInTheDocument()
  })

  // Note: admin role enforcement moved to RequireRole route guard (App.tsx).
  // Analytics component itself always fetches data — access control is at the router level.
})
