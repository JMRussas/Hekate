// Orchestration Engine - ReviewHistorySection Tests

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ReviewHistorySection from './ReviewHistorySection'
import type { TaskContextEntry } from '../types'

function makeEntry(
  verdict: 'approved' | 'changes_requested',
  issues: { severity: 'error' | 'warning'; file: string; description: string }[],
  summary: string,
): TaskContextEntry {
  return {
    type: 'review_feedback',
    content: '',
    review: { verdict, issues, summary },
  }
}

describe('ReviewHistorySection', () => {
  it('returns null when entries are empty', () => {
    const { container } = render(<ReviewHistorySection entries={[]} />)
    expect(container.innerHTML).toBe('')
  })

  it('renders single approved iteration with RESOLVED badge', () => {
    const entries = [makeEntry('approved', [], 'All looks good')]
    render(<ReviewHistorySection entries={entries} />)

    expect(screen.getByText('Review History')).toBeInTheDocument()
    expect(screen.getByText('RESOLVED')).toBeInTheDocument()
    expect(screen.getByText('1 review iteration')).toBeInTheDocument()
    expect(screen.getByText('Iteration 1')).toBeInTheDocument()
    expect(screen.getByText('approved')).toBeInTheDocument()
    expect(screen.getByText('All looks good')).toBeInTheDocument()
  })

  it('shows NEEDS REVIEW badge when last iteration is not approved', () => {
    const entries = [
      makeEntry('changes_requested', [{ severity: 'error', file: 'main.py', description: 'Bug' }], 'Fix it'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    expect(screen.getByText('NEEDS REVIEW')).toBeInTheDocument()
  })

  it('renders multiple iterations with correct count and resolved status', () => {
    const entries = [
      makeEntry('changes_requested', [{ severity: 'error', file: 'main.py', description: 'Missing null check' }], 'Needs fixes'),
      makeEntry('approved', [], 'Fixed'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    expect(screen.getByText('RESOLVED')).toBeInTheDocument()
    expect(screen.getByText(/2 review iterations/)).toBeInTheDocument()
    expect(screen.getByText(/passed after fixes/)).toBeInTheDocument()
    expect(screen.getByText('Iteration 1')).toBeInTheDocument()
    expect(screen.getByText('Iteration 2')).toBeInTheDocument()
  })

  it('collapses earlier failed iterations by default', () => {
    const entries = [
      makeEntry('changes_requested', [{ severity: 'error', file: 'main.py', description: 'Missing null check' }], 'Needs fixes'),
      makeEntry('approved', [], 'Fixed'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    // Earlier failure is collapsed — its details element should not be open
    const detailsElements = document.querySelectorAll('details')
    expect(detailsElements).toHaveLength(2)
    expect(detailsElements[0].hasAttribute('open')).toBe(false) // collapsed
    expect(detailsElements[1].hasAttribute('open')).toBe(true)  // open
  })

  it('renders changes_requested verdict with correct badge class', () => {
    const entries = [
      makeEntry('changes_requested', [], 'Problems found'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    const badge = screen.getByText('changes requested')
    expect(badge.className).toContain('needs_review')
  })

  it('renders approved verdict with correct badge class', () => {
    const entries = [makeEntry('approved', [], 'LGTM')]
    render(<ReviewHistorySection entries={entries} />)

    const badge = screen.getByText('approved')
    expect(badge.className).toContain('completed')
  })

  it('renders error severity issues with failed badge', () => {
    const entries = [
      makeEntry('changes_requested', [
        { severity: 'error', file: 'src/auth.ts', description: 'SQL injection vulnerability' },
      ], 'Critical issue'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    const severityBadge = screen.getByText('error')
    expect(severityBadge.className).toContain('failed')
    expect(screen.getByText('src/auth.ts')).toBeInTheDocument()
    expect(screen.getByText('SQL injection vulnerability')).toBeInTheDocument()
  })

  it('renders warning severity issues with pending badge', () => {
    const entries = [
      makeEntry('changes_requested', [
        { severity: 'warning', file: 'utils.py', description: 'Consider adding docstring' },
      ], 'Minor issues'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    const severityBadge = screen.getByText('warning')
    expect(severityBadge.className).toContain('pending')
  })

  it('renders mixed severities across multiple iterations', () => {
    const entries = [
      makeEntry('changes_requested', [
        { severity: 'error', file: 'api.ts', description: 'Unhandled error' },
        { severity: 'warning', file: 'api.ts', description: 'Unused import' },
        { severity: 'error', file: 'db.ts', description: 'Missing transaction' },
      ], 'Multiple issues found'),
      makeEntry('changes_requested', [
        { severity: 'warning', file: 'api.ts', description: 'Style nit' },
      ], 'Almost there'),
      makeEntry('approved', [], 'All resolved'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    expect(screen.getByText('RESOLVED')).toBeInTheDocument()
    expect(screen.getByText(/3 review iterations/)).toBeInTheDocument()
    expect(screen.getByText(/passed after fixes/)).toBeInTheDocument()

    // Earlier failures collapsed, final approved open
    const detailsElements = document.querySelectorAll('details')
    expect(detailsElements).toHaveLength(3)
    expect(detailsElements[0].hasAttribute('open')).toBe(false)
    expect(detailsElements[1].hasAttribute('open')).toBe(false)
    expect(detailsElements[2].hasAttribute('open')).toBe(true)

    // Summaries visible in summary elements (always visible)
    expect(screen.getByText('Iteration 1')).toBeInTheDocument()
    expect(screen.getByText('Iteration 2')).toBeInTheDocument()
    expect(screen.getByText('Iteration 3')).toBeInTheDocument()
  })

  it('skips entries without review data', () => {
    const entries: TaskContextEntry[] = [
      { type: 'review_feedback', content: 'no review field' },
      makeEntry('approved', [], 'Has review'),
    ]
    render(<ReviewHistorySection entries={entries} />)

    // The entry without review is skipped (returns null), but index-based
    // numbering means the second entry renders as "Iteration 2"
    expect(screen.queryByText('Iteration 1')).not.toBeInTheDocument()
    expect(screen.getByText('Iteration 2')).toBeInTheDocument()
    expect(screen.getByText('Has review')).toBeInTheDocument()
  })
})
