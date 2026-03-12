import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  getAccessToken,
  getRefreshToken,
  setTokens,
  clearTokens,
  apiRegister,
  apiLogin,
  apiRefresh,
  logout,
} from './auth'

describe('Auth API', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function mockFetchResponse(status: number, body: unknown = {}) {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    )
  }

  // -----------------------------------------------------------------------
  // Token storage
  // -----------------------------------------------------------------------

  it('stores and retrieves tokens', () => {
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()

    setTokens('access-123', 'refresh-456')
    expect(getAccessToken()).toBe('access-123')
    expect(getRefreshToken()).toBe('refresh-456')
  })

  it('clears tokens', () => {
    setTokens('a', 'b')
    clearTokens()
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
  })

  it('logout clears tokens', () => {
    setTokens('a', 'b')
    logout()
    expect(getAccessToken()).toBeNull()
  })

  // -----------------------------------------------------------------------
  // Registration
  // -----------------------------------------------------------------------

  it('registers a new user', async () => {
    const user = { id: '1', email: 'test@example.com', display_name: 'Test', role: 'admin' }
    mockFetchResponse(201, user)

    const result = await apiRegister('test@example.com', 'password123', 'Test')
    expect(result).toEqual(user)
  })

  it('throws on registration failure', async () => {
    mockFetchResponse(400, { detail: 'Registration failed' })
    await expect(apiRegister('bad', 'p')).rejects.toThrow('Registration failed')
  })

  // -----------------------------------------------------------------------
  // Login
  // -----------------------------------------------------------------------

  it('logs in and stores tokens', async () => {
    const loginResp = {
      access_token: 'acc',
      refresh_token: 'ref',
      token_type: 'bearer',
      user: { id: '1', email: 'test@example.com', display_name: 'Test', role: 'user' },
    }
    mockFetchResponse(200, loginResp)

    const result = await apiLogin('test@example.com', 'password')
    expect(result.user.email).toBe('test@example.com')
    expect(getAccessToken()).toBe('acc')
    expect(getRefreshToken()).toBe('ref')
  })

  it('throws on login failure', async () => {
    mockFetchResponse(401, { detail: 'Invalid email or password' })
    await expect(apiLogin('bad@x.com', 'wrong')).rejects.toThrow('Invalid email or password')
  })

  // -----------------------------------------------------------------------
  // Token refresh
  // -----------------------------------------------------------------------

  it('refreshes tokens successfully', async () => {
    setTokens('old-access', 'old-refresh')
    mockFetchResponse(200, { access_token: 'new-access', refresh_token: 'new-refresh' })

    const ok = await apiRefresh()
    expect(ok).toBe(true)
    expect(getAccessToken()).toBe('new-access')
    expect(getRefreshToken()).toBe('new-refresh')
  })

  it('returns false and clears tokens on refresh failure', async () => {
    setTokens('old-access', 'old-refresh')
    mockFetchResponse(401)

    const ok = await apiRefresh()
    expect(ok).toBe(false)
    expect(getAccessToken()).toBeNull()
  })

  it('returns false when no refresh token exists', async () => {
    const ok = await apiRefresh()
    expect(ok).toBe(false)
    expect(fetch).not.toHaveBeenCalled()
  })

  // -----------------------------------------------------------------------
  // Refresh deduplication
  // -----------------------------------------------------------------------

  it('deduplicates concurrent refresh calls', async () => {
    setTokens('old-access', 'old-refresh')
    // Slow response to ensure both calls share the same promise
    vi.mocked(fetch).mockImplementationOnce(async () => {
      await new Promise(r => setTimeout(r, 50))
      return new Response(
        JSON.stringify({ access_token: 'new', refresh_token: 'new-ref' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    })

    const [r1, r2] = await Promise.all([apiRefresh(), apiRefresh()])
    expect(r1).toBe(true)
    expect(r2).toBe(true)
    // Only one actual fetch call was made
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
