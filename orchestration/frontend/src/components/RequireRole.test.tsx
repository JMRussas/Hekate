import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import RequireRole from './RequireRole'

vi.mock('../hooks/useAuth', () => ({
  useAuth: vi.fn(),
}))

import { useAuth } from '../hooks/useAuth'

const mockUseAuth = vi.mocked(useAuth)

function renderWithRouter(role: string, userRole?: string, userPresent = true) {
  mockUseAuth.mockReturnValue({
    user: userPresent ? { id: '1', email: 'test@example.com', display_name: 'Test', role: userRole! } : null,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    loginWithOIDC: vi.fn(),
    setUserFromOIDC: vi.fn(),
  })

  return render(
    <MemoryRouter initialEntries={['/admin']}>
      <Routes>
        <Route element={<RequireRole role={role} />}>
          <Route path="/admin" element={<div>Admin Content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

describe('RequireRole', () => {
  it('renders outlet for correct role', () => {
    renderWithRouter('admin', 'admin')
    expect(screen.getByText('Admin Content')).toBeInTheDocument()
  })

  it('denies access for wrong role', () => {
    renderWithRouter('admin', 'user')
    expect(screen.queryByText('Admin Content')).not.toBeInTheDocument()
    expect(screen.getByText(/Access denied/)).toBeInTheDocument()
  })

  it('denies access when no user', () => {
    renderWithRouter('admin', undefined, false)
    expect(screen.queryByText('Admin Content')).not.toBeInTheDocument()
    expect(screen.getByText(/Access denied/)).toBeInTheDocument()
  })

  it('renders custom fallback', () => {
    mockUseAuth.mockReturnValue({
      user: { id: '1', email: 'a@b.com', display_name: 'X', role: 'user' },
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
      loginWithOIDC: vi.fn(),
      setUserFromOIDC: vi.fn(),
    })

    render(
      <MemoryRouter initialEntries={['/admin']}>
        <Routes>
          <Route element={<RequireRole role="admin" fallback={<div>Custom Denied</div>} />}>
            <Route path="/admin" element={<div>Admin Content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    )
    expect(screen.getByText('Custom Denied')).toBeInTheDocument()
  })
})
