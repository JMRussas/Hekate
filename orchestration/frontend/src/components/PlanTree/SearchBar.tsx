// Orchestration Engine - PlanTree Search Bar
//
// Compact search input with match count display and prev/next
// navigation. Debounces input before calling onChange.
//
// Depends on: PlanTree.css
// Used by:    PlanTree/index.tsx

import { useState, useRef, useCallback, useEffect } from 'react'

interface Props {
  query: string
  onChange: (q: string) => void
  matchCount: number
  activeMatchIndex: number
  onNext: () => void
  onPrev: () => void
}

export default function SearchBar({
  query,
  onChange,
  matchCount,
  activeMatchIndex,
  onNext,
  onPrev,
}: Props) {
  const [localValue, setLocalValue] = useState(query)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Sync local value when external query clears
  useEffect(() => {
    if (query === '') setLocalValue('')
  }, [query])

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value
    setLocalValue(val)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => onChange(val), 150)
  }, [onChange])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setLocalValue('')
      onChange('')
      return
    }
    if (e.key === 'ArrowDown' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault()
      onNext()
    }
    if (e.key === 'ArrowUp' || (e.key === 'Enter' && e.shiftKey)) {
      e.preventDefault()
      onPrev()
    }
  }, [onChange, onNext, onPrev])

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [])

  const hasQuery = query.length > 0

  return (
    <div className="pt-search">
      <input
        className="pt-search-input"
        type="text"
        placeholder="Search plan..."
        value={localValue}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        aria-label="Search plan tree"
      />
      {hasQuery && (
        <>
          <span className="pt-search-info">
            {matchCount > 0
              ? `${activeMatchIndex + 1} of ${matchCount}`
              : 'No matches'}
          </span>
          <button
            className="pt-search-nav"
            onClick={onPrev}
            disabled={matchCount === 0}
            aria-label="Previous match"
          >
            ▲
          </button>
          <button
            className="pt-search-nav"
            onClick={onNext}
            disabled={matchCount === 0}
            aria-label="Next match"
          >
            ▼
          </button>
        </>
      )}
    </div>
  )
}
