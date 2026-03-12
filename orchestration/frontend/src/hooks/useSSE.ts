// Orchestration Engine - SSE Hook
//
// Server-Sent Events hook for real-time project/task progress.
// Fetches a short-lived SSE token before connecting (never exposes
// the full access token in a URL).
//
// Reconnects with exponential backoff and fresh tokens on failure.
// Stops after MAX_RETRIES consecutive failures to avoid infinite loops.
//
// Depends on: types/index.ts, api/auth.ts, api/client.ts
// Used by:    pages/ProjectDetail.tsx

import { useEffect, useState, useRef } from 'react'
import type { SSEEvent } from '../types'
import { apiPost } from '../api/client'

const MAX_EVENTS = 200
const MAX_RETRIES = 5
const BASE_BACKOFF_MS = 1000
const MAX_BACKOFF_MS = 30000

interface SSEState {
  events: SSEEvent[]
  connected: boolean
}

const EVENT_TYPES = [
  'task_start', 'task_complete', 'task_failed', 'tool_call',
  'budget_warning', 'project_complete', 'project_failed',
  'task_retry', 'task_verification_retry', 'task_needs_review',
  'checkpoint', 'wave_checkpoint',
] as const

interface UseSSEOptions {
  onEvent?: (type: string, data: SSEEvent) => void
}

export function useSSE(projectId: string | null, options?: UseSSEOptions): SSEState {
  const [state, setState] = useState<SSEState>({ events: [], connected: false })
  const sourceRef = useRef<EventSource | null>(null)
  const onEventRef = useRef(options?.onEvent)
  onEventRef.current = options?.onEvent
  const retryCountRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!projectId) return

    retryCountRef.current = 0
    let cancelled = false

    async function connect() {
      if (cancelled) return

      try {
        // Fetch a fresh short-lived SSE token scoped to this project
        const { token } = await apiPost<{ token: string }>(
          `/events/${projectId}/token`
        )
        if (cancelled) return

        const url = `/api/events/${projectId}?token=${encodeURIComponent(token)}`
        const source = new EventSource(url)
        sourceRef.current = source

        source.onopen = () => {
          retryCountRef.current = 0
          setState(prev => ({ ...prev, connected: true }))
        }

        const handleEvent = (e: Event) => {
          try {
            const data = JSON.parse((e as MessageEvent).data) as SSEEvent
            setState(prev => ({
              ...prev,
              events: [...prev.events.slice(-MAX_EVENTS + 1), data],
            }))
            onEventRef.current?.(data.type, data)
          } catch { /* ignore parse errors */ }
        }

        for (const type of EVENT_TYPES) {
          source.addEventListener(type, handleEvent)
        }

        source.onerror = () => {
          // Close immediately — built-in auto-reconnect won't work because
          // the SSE token is short-lived and will be expired on retry
          source.close()
          sourceRef.current = null
          setState(prev => ({ ...prev, connected: false }))

          if (cancelled) return

          retryCountRef.current++
          if (retryCountRef.current > MAX_RETRIES) {
            return // Stop after too many consecutive failures
          }

          // Reconnect with exponential backoff and a fresh token
          const delay = Math.min(
            BASE_BACKOFF_MS * Math.pow(2, retryCountRef.current - 1),
            MAX_BACKOFF_MS,
          )
          reconnectTimerRef.current = setTimeout(connect, delay)
        }
      } catch {
        setState(prev => ({ ...prev, connected: false }))

        if (cancelled) return

        retryCountRef.current++
        if (retryCountRef.current > MAX_RETRIES) return

        const delay = Math.min(
          BASE_BACKOFF_MS * Math.pow(2, retryCountRef.current - 1),
          MAX_BACKOFF_MS,
        )
        reconnectTimerRef.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      cancelled = true
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (sourceRef.current) {
        sourceRef.current.close()
        sourceRef.current = null
      }
    }
  }, [projectId])

  return state
}
