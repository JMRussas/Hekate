// Orchestration Engine - Auth API
//
// Login, register, refresh, and token management.
//
// Depends on: (none)
// Used by:    hooks/useAuth.ts

const BASE = '/api/auth'

const TOKEN_KEY = 'orch_access_token'
const REFRESH_KEY = 'orch_refresh_token'

export interface User {
  id: string
  email: string
  display_name: string
  role: string
  has_password?: boolean
  linked_providers?: string[]
}

export interface OIDCProvider {
  name: string
  display_name: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
  user: User
}

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------

export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(TOKEN_KEY, access)
  localStorage.setItem(REFRESH_KEY, refresh)
}

export function clearTokens(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export async function apiRegister(
  email: string,
  password: string,
  displayName?: string
): Promise<User> {
  const resp = await fetch(`${BASE}/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, display_name: displayName || '' }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
  return resp.json()
}

export async function apiLogin(email: string, password: string): Promise<LoginResponse> {
  const resp = await fetch(`${BASE}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
  const data: LoginResponse = await resp.json()
  setTokens(data.access_token, data.refresh_token)
  return data
}

// Deduplicate concurrent refresh calls — all callers share one in-flight promise
let _refreshPromise: Promise<boolean> | null = null

export function apiRefresh(): Promise<boolean> {
  if (_refreshPromise) return _refreshPromise
  _refreshPromise = _doRefresh().finally(() => { _refreshPromise = null })
  return _refreshPromise
}

async function _doRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return false

  const resp = await fetch(`${BASE}/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  })
  if (!resp.ok) {
    clearTokens()
    return false
  }
  const data = await resp.json()
  setTokens(data.access_token, data.refresh_token)
  return true
}

export async function apiGetMe(): Promise<User> {
  const token = getAccessToken()
  const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {}

  let resp = await fetch(`${BASE}/me`, { headers })

  // If 401, try refreshing the token once (mirrors authFetch logic)
  if (resp.status === 401 && token) {
    const refreshed = await apiRefresh()
    if (refreshed) {
      const newToken = getAccessToken()
      if (newToken) {
        resp = await fetch(`${BASE}/me`, {
          headers: { Authorization: `Bearer ${newToken}` },
        })
      }
    }
  }

  if (!resp.ok) throw new Error('Not authenticated')
  return resp.json()
}

export function logout(): void {
  clearTokens()
}

// ---------------------------------------------------------------------------
// OIDC
// ---------------------------------------------------------------------------

export async function fetchOIDCProviders(): Promise<OIDCProvider[]> {
  try {
    const resp = await fetch(`${BASE}/oidc/providers`)
    if (!resp.ok) return []
    return resp.json()
  } catch {
    return []
  }
}

export async function startOIDCLogin(provider: string): Promise<void> {
  const redirectUri = `${window.location.origin}/auth/oidc/callback`
  const resp = await fetch(
    `${BASE}/oidc/${provider}/login?redirect_uri=${encodeURIComponent(redirectUri)}`
  )
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: 'OIDC error' }))
    throw new Error(err.detail || 'Failed to start OIDC login')
  }
  const data = await resp.json()
  sessionStorage.setItem('oidc_state_token', data.state_token)
  sessionStorage.setItem('oidc_provider', provider)
  window.location.href = data.authorization_url
}

export async function completeOIDCLogin(
  provider: string,
  code: string,
  state: string,
): Promise<LoginResponse> {
  const stateToken = sessionStorage.getItem('oidc_state_token')
  if (!stateToken) throw new Error('Missing state token')

  const redirectUri = `${window.location.origin}/auth/oidc/callback`
  const resp = await fetch(`${BASE}/oidc/${provider}/callback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code,
      state,
      state_token: stateToken,
      redirect_uri: redirectUri,
    }),
  })

  sessionStorage.removeItem('oidc_state_token')
  sessionStorage.removeItem('oidc_provider')

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: 'OIDC callback failed' }))
    throw new Error(err.detail || 'OIDC authentication failed')
  }

  const data: LoginResponse = await resp.json()
  setTokens(data.access_token, data.refresh_token)
  return data
}
