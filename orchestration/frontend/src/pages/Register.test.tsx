import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const mockLogin = vi.fn()

vi.mock('../api/auth', () => ({
  apiRegister: vi.fn(),
  fetchOIDCProviders: vi.fn().mockResolvedValue([]),
}))
vi.mock('../hooks/useAuth', () => ({
  useAuth: () => ({
    user: null,
    loading: false,
    login: mockLogin,
    loginWithOIDC: vi.fn(),
    logout: vi.fn(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

import { apiRegister, fetchOIDCProviders } from '../api/auth'
import Register from './Register'

const mockApiRegister = vi.mocked(apiRegister)
const mockFetchOIDCProviders = vi.mocked(fetchOIDCProviders)

function renderRegister() {
  return render(
    <MemoryRouter initialEntries={['/register']}>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/" element={<div>Dashboard</div>} />
        <Route path="/login" element={<div>Login Page</div>} />
      </Routes>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.resetAllMocks()
  mockFetchOIDCProviders.mockResolvedValue([])
})

describe('Register', () => {
  it('renders form fields and button', () => {
    renderRegister()
    expect(screen.getByRole('button', { name: 'Create Account' })).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByLabelText('Display Name')).toBeInTheDocument()
  })

  it('successful register + auto-login navigates to dashboard', async () => {
    mockApiRegister.mockResolvedValue({ id: 'u1', email: 'a@b.com', display_name: 'A', role: 'user' })
    mockLogin.mockResolvedValue(undefined)
    renderRegister()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Email'), 'a@b.com')
    await user.type(screen.getByLabelText('Password'), 'password123')
    await user.click(screen.getByRole('button', { name: 'Create Account' }))

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument()
    })
    expect(mockApiRegister).toHaveBeenCalled()
    expect(mockLogin).toHaveBeenCalledWith('a@b.com', 'password123')
  })

  it('failed register shows error', async () => {
    mockApiRegister.mockRejectedValue(new Error('Email already exists'))
    renderRegister()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Email'), 'a@b.com')
    await user.type(screen.getByLabelText('Password'), 'password123')
    await user.click(screen.getByRole('button', { name: 'Create Account' }))

    await waitFor(() => {
      expect(screen.getByText('Email already exists')).toBeInTheDocument()
    })
  })

  it('shows loading state', async () => {
    mockApiRegister.mockReturnValue(new Promise(() => {}))
    renderRegister()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Email'), 'a@b.com')
    await user.type(screen.getByLabelText('Password'), 'password123')
    await user.click(screen.getByRole('button', { name: 'Create Account' }))

    await waitFor(() => {
      expect(screen.getByText('Creating account...')).toBeInTheDocument()
    })
  })

  it('has login link', () => {
    renderRegister()
    expect(screen.getByText('Sign In')).toBeInTheDocument()
  })
})
