// Pipeline Observatory — event polling hook
//
// Polls /api/events/{projectId} for god_relay_events.
// Accumulates events, manages cursor, auto-polls every 2s.

import { useEffect, useState, useRef, useCallback } from 'react'
import { apiFetch } from '../api/client'
import type { RelayEvent } from '../types'

interface UseObservatoryResult {
  events: RelayEvent[]
  connected: boolean
  cursor: number
  clear: () => void
}

export function useObservatoryEvents(projectId: string | null): UseObservatoryResult {
  const [events, setEvents] = useState<RelayEvent[]>([])
  const [connected, setConnected] = useState(false)
  const cursorRef = useRef(0)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = useCallback(async () => {
    if (!projectId) return
    try {
      const batch = await apiFetch<RelayEvent[]>(
        `/events/${projectId}?since=${cursorRef.current}&limit=100`
      )
      if (batch.length > 0) {
        // Parse payload if it's a string
        const parsed = batch.map(ev => ({
          ...ev,
          payload: typeof ev.payload === 'string' ? JSON.parse(ev.payload) : ev.payload,
        }))
        cursorRef.current = Math.max(cursorRef.current, ...parsed.map(e => e.id))
        setEvents(prev => [...prev, ...parsed])
      }
      setConnected(true)
    } catch {
      setConnected(false)
    }
  }, [projectId])

  useEffect(() => {
    if (!projectId) return
    // Initial fetch
    cursorRef.current = 0
    setEvents([])
    poll()

    // Poll every 2s
    intervalRef.current = setInterval(poll, 2000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [projectId, poll])

  const clear = useCallback(() => {
    setEvents([])
    cursorRef.current = 0
  }, [])

  return { events, connected, cursor: cursorRef.current, clear }
}
