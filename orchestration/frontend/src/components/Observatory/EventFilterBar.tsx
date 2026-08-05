// Filter bar for Observatory — category chips, search, follow-tail toggle

import type { EventCategory, EventFilters } from '../../types'

interface EventFilterBarProps {
  filters: EventFilters
  onChange: (filters: EventFilters) => void
  eventTypeCounts: Record<string, number>
}

const CATEGORIES: { key: EventCategory; label: string }[] = [
  { key: 'planning', label: 'Planning' },
  { key: 'dispatch', label: 'Dispatch' },
  { key: 'execution', label: 'Execution' },
  { key: 'verification', label: 'Verification' },
  { key: 'gate', label: 'Gates' },
  { key: 'lifecycle', label: 'Lifecycle' },
  { key: 'error', label: 'Errors' },
  { key: 'budget', label: 'Budget' },
]

export function EventFilterBar({ filters, onChange, eventTypeCounts }: EventFilterBarProps) {
  const toggleCategory = (cat: EventCategory) => {
    const next = new Set(filters.categories)
    if (next.has(cat)) next.delete(cat)
    else next.add(cat)
    onChange({ ...filters, categories: next })
  }

  const toggleFollowTail = () => {
    onChange({ ...filters, followTail: !filters.followTail })
  }

  return (
    <div className="filter-bar">
      {CATEGORIES.map(({ key, label }) => {
        const count = eventTypeCounts[key] || 0
        return (
          <button
            key={key}
            className={`filter-chip ${filters.categories.has(key) ? 'active' : ''} ${key}`}
            onClick={() => toggleCategory(key)}
          >
            {label}
            {count > 0 && <span className="count">{count}</span>}
          </button>
        )
      })}

      <div className="filter-spacer" />

      <input
        type="text"
        className="filter-search"
        placeholder="Search events..."
        value={filters.search}
        onChange={(e) => onChange({ ...filters, search: e.target.value })}
      />

      <button
        className={`filter-chip ${filters.followTail ? 'active' : ''}`}
        onClick={toggleFollowTail}
        title="Auto-scroll to latest events"
      >
        Follow
      </button>
    </div>
  )
}
