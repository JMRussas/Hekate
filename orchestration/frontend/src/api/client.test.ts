import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Mock auth module before importing client
vi.mock('./auth', () => ({
  getAccessToken: vi.fn(),
  apiRefresh: vi.fn(),
}))

import { apiFetch, apiPost, apiPatch, apiDelete } from './client'
import { getAccessToken, apiRefresh } from './auth'

const mockGetToken = vi.mocked(getAccessToken)
const mockRefresh = vi.mocked(apiRefresh)

describe('API Client', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    // Default: authenticated
    mockGetToken.mockReturnValue('test-token')
    // Mock global fetch
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
  // Token injection
  // -----------------------------------------------------------------------

  it('injects Authorization header when token exists', async () => {
    mockFetchResponse(200, { id: '1' })
    await apiFetch('/projects')

    const [url, opts] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/projects')
    const headers = opts?.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer test-token')
  })

  it('omits Authorization when no token', async () => {
    mockGetToken.mockReturnValue(null)
    mockFetchResponse(200, { id: '1' })
    await apiFetch('/projects')

    const [, opts] = vi.mocked(fetch).mock.calls[0]
    const headers = opts?.headers as Headers
    expect(headers.get('Authorization')).toBeNull()
  })

  // -----------------------------------------------------------------------
  // 401 retry
  // -----------------------------------------------------------------------

  it('retries once on 401 after successful refresh', async () => {
    // First call returns 401, retry returns 200
    mockFetchResponse(401)
    mockRefresh.mockResolvedValueOnce(true)
    mockGetToken.mockReturnValueOnce('test-token').mockReturnValueOnce('new-token')
    mockFetchResponse(200, { data: 'ok' })

    const result = await apiFetch<{ data: string }>('/projects')
    expect(result).toEqual({ data: 'ok' })
    expect(mockRefresh).toHaveBeenCalledOnce()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('throws on 401 when refresh fails', async () => {
    mockFetchResponse(401, { detail: 'Unauthorized' })
    mockRefresh.mockResolvedValueOnce(false)

    await expect(apiFetch('/projects')).rejects.toThrow('Unauthorized')
  })

  // -----------------------------------------------------------------------
  // HTTP methods
  // -----------------------------------------------------------------------

  it('apiPost sends JSON body', async () => {
    mockFetchResponse(200, { id: 'new' })
    await apiPost('/projects', { name: 'Test' })

    const [, opts] = vi.mocked(fetch).mock.calls[0]
    expect(opts?.method).toBe('POST')
    expect(opts?.body).toBe(JSON.stringify({ name: 'Test' }))
  })

  it('apiPatch sends JSON body', async () => {
    mockFetchResponse(200, { id: '1' })
    await apiPatch('/projects/1', { name: 'Updated' })

    const [, opts] = vi.mocked(fetch).mock.calls[0]
    expect(opts?.method).toBe('PATCH')
  })

  it('apiDelete handles 204 response', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(null, { status: 204 })
    )
    await expect(apiDelete('/projects/1')).resolves.toBeUndefined()
  })

  // -----------------------------------------------------------------------
  // Error handling
  // -----------------------------------------------------------------------

  it('throws with detail from error response', async () => {
    mockFetchResponse(400, { detail: 'Bad request' })
    await expect(apiFetch('/projects')).rejects.toThrow('Bad request')
  })

  it('falls back to statusText when body is not JSON', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('not json', { status: 500, statusText: 'Server Error' })
    )
    await expect(apiFetch('/projects')).rejects.toThrow('Server Error')
  })
})
