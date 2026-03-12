// Orchestration Engine - API Client
//
// Base fetch wrapper for the REST API.
// Injects JWT Authorization header and handles token refresh on 401.
//
// Depends on: api/auth.ts
// Used by:    api/projects.ts, api/tasks.ts, api/usage.ts, api/services.ts

import { getAccessToken, apiRefresh } from './auth'

const BASE = '/api'

export async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = getAccessToken()
  const headers = new Headers(init?.headers)
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  let resp = await fetch(`${BASE}${path}`, { ...init, headers })

  // If 401, try refreshing the token once
  if (resp.status === 401 && token) {
    const refreshed = await apiRefresh()
    if (refreshed) {
      const newToken = getAccessToken()
      if (newToken) {
        headers.set('Authorization', `Bearer ${newToken}`)
        resp = await fetch(`${BASE}${path}`, { ...init, headers })
      }
    }
  }

  return resp
}

export async function apiFetch<T>(path: string): Promise<T> {
  const resp = await authFetch(path)
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
  return resp.json()
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const resp = await authFetch(path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
  return resp.json()
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const resp = await authFetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
  return resp.json()
}

export async function apiDelete(path: string): Promise<void> {
  const resp = await authFetch(path, { method: 'DELETE' })
  if (!resp.ok && resp.status !== 204) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || resp.statusText)
  }
}
