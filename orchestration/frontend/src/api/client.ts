// Hekate - API Client
//
// Simple fetch wrapper. No auth.

const BASE = '/api'

export async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${BASE}${path}`, { ...init })
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
