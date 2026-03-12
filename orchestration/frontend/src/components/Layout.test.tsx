import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

const mockLogout = vi.fn()

vi.mock('../hooks/useAuth', () => ({
  useAuth: () => ({
    user: { id: 'u1', email: 'a@b.com', display_name: 'Test User', role: 'user' },
    loading: false,
    login: vi.fn(),
    logout: mockLogout,
  }),
}))

import Layout from './Layout'

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Layout />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('Layout', () => {
  it('renders sidebar nav links', () => {
    renderLayout()
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Usage & Budget')).toBeInTheDocument()
    expect(screen.getByText('Services')).toBeInTheDocument()
  })

  it('shows user display name', () => {
    renderLayout()
    expect(screen.getByText('Test User')).toBeInTheDocument()
  })

  it('shows sign out button that calls logout', async () => {
    renderLayout()

    const user = userEvent.setup()
    await user.click(screen.getByText('Sign Out'))

    expect(mockLogout).toHaveBeenCalledTimes(1)
  })

  it('renders app title', () => {
    renderLayout()
    expect(screen.getByText('Orchestration')).toBeInTheDocument()
  })
})
