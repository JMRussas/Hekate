import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

vi.mock('../api/client', () => ({
  apiPost: vi.fn(),
}))

import { apiPost } from '../api/client'
import { useSSE } from './useSSE'

const mockApiPost = vi.mocked(apiPost)

// Mock EventSource
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  listeners: Record<string, ((e: Event) => void)[]> = {}
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, handler: (e: Event) => void) {
    if (!this.listeners[type]) this.listeners[type] = []
    this.listeners[type].push(handler)
  }

  close() {
    this.closed = true
  }

  // Test helpers
  _fireOpen() {
    this.onopen?.()
  }

  _fireError() {
    this.onerror?.()
  }

  _fireEvent(type: string, data: unknown) {
    const handlers = this.listeners[type] || []
    for (const h of handlers) {
      h({ data: JSON.stringify(data) } as unknown as Event)
    }
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useSSE', () => {
  it('null projectId → no connection', () => {
    const { result } = renderHook(() => useSSE(null))

    expect(result.current.connected).toBe(false)
    expect(result.current.events).toEqual([])
    expect(MockEventSource.instances).toHaveLength(0)
  })

  it('connects and creates EventSource with correct URL', async () => {
    mockApiPost.mockResolvedValue({ token: 'sse-tok-123' })

    renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    expect(mockApiPost).toHaveBeenCalledWith('/events/proj_001/token')
    expect(MockEventSource.instances[0].url).toBe(
      '/api/events/proj_001?token=sse-tok-123'
    )
  })

  it('onopen sets connected to true', async () => {
    mockApiPost.mockResolvedValue({ token: 'tok' })

    const { result } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    act(() => {
      MockEventSource.instances[0]._fireOpen()
    })

    expect(result.current.connected).toBe(true)
  })

  it('event received is parsed and appended', async () => {
    mockApiPost.mockResolvedValue({ token: 'tok' })

    const { result } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    const eventData = { type: 'task_start', message: 'Starting', project_id: 'proj_001', task_id: 't1', timestamp: 123 }
    act(() => {
      MockEventSource.instances[0]._fireEvent('task_start', eventData)
    })

    expect(result.current.events).toHaveLength(1)
    expect(result.current.events[0].type).toBe('task_start')
  })

  it('onerror closes source and sets disconnected', async () => {
    mockApiPost.mockResolvedValue({ token: 'tok' })

    const { result } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    act(() => {
      MockEventSource.instances[0]._fireOpen()
    })
    expect(result.current.connected).toBe(true)

    // Trigger error — should close source immediately (not leave open for
    // auto-reconnect, which would fail with an expired SSE token)
    act(() => {
      MockEventSource.instances[0]._fireError()
    })
    expect(result.current.connected).toBe(false)
    expect(MockEventSource.instances[0].closed).toBe(true)
  })

  it('reconnects with fresh token after error', async () => {
    mockApiPost
      .mockResolvedValueOnce({ token: 'tok1' })
      .mockResolvedValueOnce({ token: 'tok2' })

    renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    // Trigger error to start reconnect
    act(() => {
      MockEventSource.instances[0]._fireError()
    })

    // Wait for the backoff timer + reconnect to create a new EventSource
    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(2)
    }, { timeout: 3000 })

    // Second connection should use a fresh token
    expect(mockApiPost).toHaveBeenCalledTimes(2)
    expect(MockEventSource.instances[1].url).toContain('tok2')
  })

  it('onopen resets retry count', async () => {
    mockApiPost.mockResolvedValue({ token: 'tok' })

    const { result } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    // Error then reconnect
    act(() => {
      MockEventSource.instances[0]._fireError()
    })

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(2)
    }, { timeout: 3000 })

    // Successful open should reset retry count
    act(() => {
      MockEventSource.instances[1]._fireOpen()
    })
    expect(result.current.connected).toBe(true)
  })

  it('cleanup on unmount closes source', async () => {
    mockApiPost.mockResolvedValue({ token: 'tok' })

    const { unmount } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(MockEventSource.instances).toHaveLength(1)
    })

    unmount()
    expect(MockEventSource.instances[0].closed).toBe(true)
  })

  it('token fetch failure → connected false, retries later', async () => {
    mockApiPost.mockRejectedValue(new Error('Auth failed'))

    const { result } = renderHook(() => useSSE('proj_001'))

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalled()
    })

    expect(result.current.connected).toBe(false)
    expect(MockEventSource.instances).toHaveLength(0)
    // Will retry with backoff — first attempt already counted
    expect(mockApiPost).toHaveBeenCalledTimes(1)
  })
})
