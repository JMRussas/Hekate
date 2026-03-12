import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../api/auth', () => ({
  apiLogin: vi.fn(),
  apiRefresh: vi.fn(),
  apiGetMe: vi.fn(),
  getAccessToken: vi.fn(),
  setTokens: vi.fn(),
  clearTokens: vi.fn(),
}))

import {
  apiLogin, apiRefresh, apiGetMe,
  getAccessToken, setTokens, clearTokens,
} from '../api/auth'
import { AuthProvider, useAuth } from './useAuth'

const mockApiLogin = vi.mocked(apiLogin)
const mockApiRefresh = vi.mocked(apiRefresh)
const mockApiGetMe = vi.mocked(apiGetMe)
const mockGetAccessToken = vi.mocked(getAccessToken)
const mockClearTokens = vi.mocked(clearTokens)

const testUser = { id: 'u1', email: 'test@test.com', display_name: 'Test', role: 'user' }

function TestConsumer() {
  const { user, loading, login, logout } = useAuth()
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="user">{user ? user.display_name : 'null'}</span>
      <button onClick={() => login('test@test.com', 'pass123')}>Login</button>
      <button onClick={logout}>Logout</button>
    </div>
  )
}

function renderWithAuth() {
  return render(
    <AuthProvider>
      <TestConsumer />
    </AuthProvider>
  )
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('AuthProvider', () => {
  it('no token → loading becomes false, user null', async () => {
    mockGetAccessToken.mockReturnValue(null)
    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false')
    })
    expect(screen.getByTestId('user').textContent).toBe('null')
  })

  it('token exists + apiGetMe succeeds → user set', async () => {
    mockGetAccessToken.mockReturnValue('tok123')
    mockApiGetMe.mockResolvedValue(testUser)

    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('Test')
    })
    expect(screen.getByTestId('loading').textContent).toBe('false')
  })

  it('token exists + apiGetMe fails + apiRefresh succeeds → retries', async () => {
    mockGetAccessToken.mockReturnValue('tok123')
    mockApiGetMe
      .mockRejectedValueOnce(new Error('expired'))
      .mockResolvedValueOnce(testUser)
    mockApiRefresh.mockResolvedValue(true)

    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('Test')
    })
    expect(mockApiRefresh).toHaveBeenCalledTimes(1)
    expect(mockApiGetMe).toHaveBeenCalledTimes(2)
  })

  it('token exists + all fail → clearTokens called', async () => {
    mockGetAccessToken.mockReturnValue('tok123')
    mockApiGetMe.mockRejectedValue(new Error('expired'))
    mockApiRefresh.mockResolvedValue(false)

    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false')
    })
    expect(mockClearTokens).toHaveBeenCalled()
    expect(screen.getByTestId('user').textContent).toBe('null')
  })

  it('login() calls apiLogin and sets user', async () => {
    mockGetAccessToken.mockReturnValue(null)
    mockApiLogin.mockResolvedValue({
      access_token: 'a', refresh_token: 'r', token_type: 'bearer', user: testUser,
    })

    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false')
    })

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(screen.getByText('Login'))

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('Test')
    })
    expect(mockApiLogin).toHaveBeenCalledWith('test@test.com', 'pass123')
  })

  it('logout() clears user and tokens', async () => {
    mockGetAccessToken.mockReturnValue('tok123')
    mockApiGetMe.mockResolvedValue(testUser)

    renderWithAuth()

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('Test')
    })

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(screen.getByText('Logout'))

    expect(screen.getByTestId('user').textContent).toBe('null')
    expect(mockClearTokens).toHaveBeenCalled()
  })
})
