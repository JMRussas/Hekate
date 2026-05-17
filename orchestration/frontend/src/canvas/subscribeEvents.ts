// Hekate canvas — production SSE subscription.
//
// Implements the SubscribeEvents contract that ProjectCanvas takes as a
// dep. Opens an EventSource against the orchestration's /api/events/{id}
// endpoint (after fetching a short-lived SSE token), routes parsed
// payloads to the callback, returns an unsubscribe.
//
// Reconnect with backoff is deliberately *not* included here — the
// existing useSSE hook in hooks/useSSE.ts has that logic, and this
// module is intentionally simple while the canvas is still maturing.
// If a connection drops, the user re-navigates to see the latest state.
// Adding reconnect is its own follow-up cycle.

import { apiPost } from '../api/client'
import type { SSEEvent } from '../types'
import type { SubscribeEvents } from './ProjectCanvas'

// Event types the orchestration publishes. Must register a listener per
// type (EventSource doesn't fire a generic "message" listener for named
// events). Kept in sync with backend/services/progress.py emitters.
const EVENT_TYPES = [
  'task_start',
  'task_complete',
  'task_failed',
  'tool_call',
  'task_output',
  'task_error',
  'budget_warning',
  'project_complete',
  'project_failed',
  'task_retry',
  'task_verification_retry',
  'task_needs_review',
  'checkpoint',
  'wave_checkpoint',
] as const

export const subscribeEvents: SubscribeEvents = (projectId, onEvent) => {
  let source: EventSource | null = null
  let cancelled = false

  async function open() {
    try {
      const { token } = await apiPost<{ token: string }>(
        `/events/${projectId}/token`,
      )
      if (cancelled) return

      const url = `/api/events/${projectId}?token=${encodeURIComponent(token)}`
      const s = new EventSource(url)
      source = s

      const handle = (e: Event) => {
        try {
          const data = JSON.parse((e as MessageEvent).data) as SSEEvent
          onEvent(data)
        } catch {
          // malformed event — drop silently; live stream is best-effort
        }
      }
      for (const t of EVENT_TYPES) s.addEventListener(t, handle)
    } catch {
      // token fetch / EventSource open failed; canvas stays on the
      // initial snapshot until the next reload
    }
  }

  void open()

  return () => {
    cancelled = true
    source?.close()
    source = null
  }
}
