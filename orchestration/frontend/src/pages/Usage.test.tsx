import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../api/usage', () => ({
  getBudget: vi.fn(),
  getUsageSummary: vi.fn(),
  getDailyUsage: vi.fn(),
  getUsageByProject: vi.fn(),
}))

import { getBudget, getUsageSummary, getDailyUsage, getUsageByProject } from '../api/usage'
import Usage from './Usage'

const mockGetBudget = vi.mocked(getBudget)
const mockGetUsageSummary = vi.mocked(getUsageSummary)
const mockGetDailyUsage = vi.mocked(getDailyUsage)
const mockGetUsageByProject = vi.mocked(getUsageByProject)

function setupMocks(overrides: Record<string, unknown> = {}) {
  mockGetBudget.mockResolvedValue({
    daily_spent_usd: 1.5, daily_limit_usd: 5.0, daily_pct: 30,
    monthly_spent_usd: 10.0, monthly_limit_usd: 50.0, monthly_pct: 20,
    per_project_limit_usd: 10.0,
    ...overrides,
  } as ReturnType<typeof getBudget> extends Promise<infer T> ? T : never)
  mockGetUsageSummary.mockResolvedValue({
    total_cost_usd: 2.5, api_call_count: 10,
    total_prompt_tokens: 5000, total_completion_tokens: 3000,
    by_model: { 'claude-haiku': { calls: 5, prompt_tokens: 2500, completion_tokens: 1500, cost_usd: 1.25 } },
  } as ReturnType<typeof getUsageSummary> extends Promise<infer T> ? T : never)
  mockGetDailyUsage.mockResolvedValue([
    { date: '2025-01-01', cost_usd: 0.5, api_calls: 3 },
  ])
  mockGetUsageByProject.mockResolvedValue([
    { project_id: 'p1', project_name: 'Project A', cost_usd: 1.5 },
  ])
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('Usage', () => {
  it('renders daily spend card', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('$1.50')).toBeInTheDocument()
    expect(screen.getByText(/\/ \$5\.00/)).toBeInTheDocument()
  })

  it('renders monthly spend card', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('$10.00')).toBeInTheDocument()
    expect(screen.getByText(/\/ \$50\.00/)).toBeInTheDocument()
  })

  it('progress bar class ok for low percentage', async () => {
    setupMocks()
    render(<Usage />)
    await waitFor(() => {
      const fills = document.querySelectorAll('.progress-fill')
      expect(fills.length).toBeGreaterThan(0)
      expect(fills[0]).toHaveClass('ok')
    })
  })

  it('by-model table renders', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('claude-haiku')).toBeInTheDocument()
  })

  it('by-model empty shows no usage', async () => {
    setupMocks()
    mockGetUsageSummary.mockResolvedValue({
      total_cost_usd: 0, api_call_count: 0,
      total_prompt_tokens: 0, total_completion_tokens: 0, by_model: {},
    } as ReturnType<typeof getUsageSummary> extends Promise<infer T> ? T : never)
    render(<Usage />)
    const noUsages = await screen.findAllByText('No usage yet')
    expect(noUsages.length).toBeGreaterThanOrEqual(1)
  })

  it('by-project table renders', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('Project A')).toBeInTheDocument()
  })

  it('daily history table shown', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('2025-01-01')).toBeInTheDocument()
  })

  it('totals section shows aggregate', async () => {
    setupMocks()
    render(<Usage />)
    expect(await screen.findByText('$2.5000')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
  })

  it('shows error on any failure', async () => {
    mockGetBudget.mockRejectedValue(new Error('Server error'))
    mockGetUsageSummary.mockRejectedValue(new Error('Server error'))
    mockGetDailyUsage.mockRejectedValue(new Error('Server error'))
    mockGetUsageByProject.mockRejectedValue(new Error('Server error'))

    render(<Usage />)
    expect(await screen.findByText(/Failed to load usage data/)).toBeInTheDocument()
  })
})
