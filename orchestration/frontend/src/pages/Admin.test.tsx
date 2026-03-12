// Orchestration Engine - Admin Page Tests

import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import Admin from './Admin'

vi.mock('../api/admin', () => ({
  listUsers: vi.fn().mockResolvedValue([
    {
      id: 'u1', email: 'admin@test.com', display_name: 'Admin',
      role: 'admin', is_active: true, created_at: 1700000000,
      last_login_at: 1700001000, project_count: 3,
    },
    {
      id: 'u2', email: 'user@test.com', display_name: 'User',
      role: 'user', is_active: false, created_at: 1700002000,
      last_login_at: null, project_count: 0,
    },
  ]),
  updateUser: vi.fn().mockResolvedValue({}),
  getStats: vi.fn().mockResolvedValue({
    total_users: 2, active_users: 1,
    total_projects: 3, projects_by_status: { draft: 1, executing: 2 },
    total_tasks: 10, tasks_by_status: { completed: 7, failed: 1, pending: 2 },
    total_spend_usd: 0.1234, spend_by_model: { 'claude-haiku': 0.05, 'claude-sonnet': 0.0734 },
    task_completion_rate: 0.875,
  }),
}))

function renderAdmin() {
  return render(
    <BrowserRouter>
      <Admin />
    </BrowserRouter>
  )
}

describe('Admin', () => {
  it('renders heading', () => {
    renderAdmin()
    expect(screen.getByText('Admin Dashboard')).toBeInTheDocument()
  })

  it('shows users table after loading', async () => {
    renderAdmin()
    await waitFor(() => {
      expect(screen.getByText('admin@test.com')).toBeInTheDocument()
    })
    expect(screen.getByText('user@test.com')).toBeInTheDocument()
  })

  it('shows stats after loading', async () => {
    renderAdmin()
    await waitFor(() => {
      expect(screen.getByText('2')).toBeInTheDocument() // total_users
    })
    expect(screen.getByText('$0.1234')).toBeInTheDocument()
  })

  it('shows model spend breakdown', async () => {
    renderAdmin()
    await waitFor(() => {
      expect(screen.getByText('claude-haiku')).toBeInTheDocument()
    })
    expect(screen.getByText('claude-sonnet')).toBeInTheDocument()
  })

  it('shows deactivate button for active users', async () => {
    renderAdmin()
    await waitFor(() => {
      expect(screen.getByText('admin@test.com')).toBeInTheDocument()
    })
    const buttons = screen.getAllByText('Deactivate')
    expect(buttons.length).toBeGreaterThan(0)
  })
})
