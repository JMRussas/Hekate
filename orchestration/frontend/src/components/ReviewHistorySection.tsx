// Orchestration Engine - Review History Section
//
// Displays code review iteration history: verdict badges,
// issue lists with severity coloring, and summary text.
//
// Depends on: types/index.ts (ReviewFeedback, ReviewIssue, ContextEntry)
// Used by:    pages/TaskDetail.tsx

import type { TaskContextEntry } from '../types'

interface ReviewHistorySectionProps {
  entries: TaskContextEntry[]
}

export default function ReviewHistorySection({ entries }: ReviewHistorySectionProps) {
  if (entries.length === 0) return null

  return (
    <div className="card">
      <h2>Review History</h2>
      <p className="text-dim text-sm mb-1">
        {entries.length} review iteration{entries.length !== 1 ? 's' : ''}
      </p>
      {entries.map((entry, i) => {
        const review = entry.review
        if (!review) return null

        const isApproved = review.verdict === 'approved'
        const verdictClass = isApproved ? 'completed' : 'needs_review'

        return (
          <div
            key={i}
            className="verification-card"
            style={{
              borderLeftColor: isApproved ? 'var(--success)' : 'var(--warning)',
            }}
          >
            <div className="flex-between mb-1">
              <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>
                Iteration {i + 1}
              </span>
              <span className={`badge ${verdictClass}`}>
                {review.verdict.replace('_', ' ')}
              </span>
            </div>

            {review.issues.length > 0 && (
              <div style={{ marginBottom: '0.5rem' }}>
                {review.issues.map((issue, j) => (
                  <div
                    key={j}
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: '0.5rem',
                      padding: '0.25rem 0',
                      fontSize: '0.8rem',
                    }}
                  >
                    <span
                      className={`badge ${issue.severity === 'error' ? 'failed' : 'pending'}`}
                    >
                      {issue.severity}
                    </span>
                    <span className="text-mono" style={{ color: 'var(--accent)', flexShrink: 0 }}>
                      {issue.file}
                    </span>
                    <span>{issue.description}</span>
                  </div>
                ))}
              </div>
            )}

            <p className="text-dim text-sm">{review.summary}</p>
          </div>
        )
      })}
    </div>
  )
}
