// Single event row in the Observatory timeline

import { useState } from 'react'
import type { RelayEvent } from '../../types'
import { getEventCategory, getEventSummary } from './categories'

interface EventCardProps {
  event: RelayEvent
  isHighlighted: boolean
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
    + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

export function EventCard({ event, isHighlighted }: EventCardProps) {
  const [expanded, setExpanded] = useState(false)
  const category = getEventCategory(event.event_type)
  const summary = getEventSummary(event.event_type, event.payload)

  return (
    <div
      className={`event-card ${expanded ? 'expanded' : ''} ${isHighlighted ? 'highlighted' : ''}`}
      onClick={() => setExpanded(!expanded)}
    >
      <span className="event-timestamp">{formatTime(event.created_at)}</span>
      <span className={`event-badge ${category}`}>{event.event_type}</span>
      <span className="event-source">{event.source}</span>
      <span className="event-summary">{summary}</span>

      {expanded && (
        <div className="event-detail">
          <pre>{JSON.stringify(event.payload, null, 2)}</pre>
          <div className="event-meta">
            <span>ID: {event.id}</span>
            <span>Severity: {event.severity}</span>
          </div>
        </div>
      )}
    </div>
  )
}
