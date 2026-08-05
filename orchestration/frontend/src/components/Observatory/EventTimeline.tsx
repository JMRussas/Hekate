// Chronological event log with filtering and auto-scroll

import { useEffect, useRef, useMemo } from 'react'
import type { RelayEvent, EventFilters } from '../../types'
import { getEventCategory } from './categories'
import { EventCard } from './EventCard'

interface EventTimelineProps {
  events: RelayEvent[]
  filters: EventFilters
  highlightTaskId: string | null
}

export function EventTimeline({ events, filters, highlightTaskId }: EventTimelineProps) {
  const endRef = useRef<HTMLDivElement>(null)

  const filtered = useMemo(() => {
    return events.filter(ev => {
      // Category filter
      if (filters.categories.size > 0) {
        const cat = getEventCategory(ev.event_type)
        if (!filters.categories.has(cat)) return false
      }

      // Severity filter
      if (filters.severities.size > 0 && !filters.severities.has(ev.severity)) {
        return false
      }

      // Source filter
      if (filters.sources.size > 0 && !filters.sources.has(ev.source)) {
        return false
      }

      // Text search
      if (filters.search) {
        const q = filters.search.toLowerCase()
        const haystack = `${ev.event_type} ${ev.source} ${JSON.stringify(ev.payload)}`.toLowerCase()
        if (!haystack.includes(q)) return false
      }

      // Task highlight filter (when a task is selected, show only its events + global events)
      if (highlightTaskId) {
        const payload = ev.payload || {}
        const taskId = payload.task_id as string
        if (taskId && !taskId.startsWith(highlightTaskId)) return true
        // Also show project-level events (no task_id)
        if (!taskId) return true
        return false
      }

      return true
    })
  }, [events, filters, highlightTaskId])

  // Auto-scroll to bottom when follow-tail is on
  useEffect(() => {
    if (filters.followTail && endRef.current) {
      endRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [filtered.length, filters.followTail])

  if (filtered.length === 0) {
    return (
      <div className="event-timeline-empty">
        <p className="text-dim">No events yet. Events will appear as the pipeline processes this project.</p>
      </div>
    )
  }

  return (
    <div className="event-timeline">
      {filtered.map(ev => (
        <EventCard
          key={ev.id}
          event={ev}
          isHighlighted={
            !!highlightTaskId &&
            (ev.payload?.task_id as string)?.startsWith(highlightTaskId)
          }
        />
      ))}
      <div ref={endRef} />
    </div>
  )
}
