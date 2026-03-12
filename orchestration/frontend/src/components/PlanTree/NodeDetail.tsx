// Orchestration Engine - NodeDetail Panel
//
// Slide-in panel showing full details for a selected tree node.
// Uses aria-live to announce content changes to screen readers.
//
// Depends on: types.ts, theme.ts
// Used by:    PlanTree/index.tsx

import { useRef } from 'react'
import type { TreeNode } from './types'
import type { PlanTreeTheme } from './theme'
import { getNodeColors } from './theme'

interface Props {
  node: TreeNode | null
  theme: PlanTreeTheme
  onClose: () => void
}

export default function NodeDetail({ node, theme, onClose }: Props) {
  const panelRef = useRef<HTMLDivElement>(null)

  // Escape within the panel closes it. This is scoped to the panel's
  // onKeyDown (not a global listener) to avoid double-firing with
  // useTreeKeyboard's Escape handler on the tree container.

  if (!node?.detail) return null

  const colors = getNodeColors(theme, node.type)

  return (
    <div
      ref={panelRef}
      className="pt-detail-panel"
      style={{ borderLeftColor: colors.accent }}
      aria-live="polite"
      aria-label="Node details"
      onKeyDown={e => { if (e.key === 'Escape') onClose() }}
    >
      <div className="pt-detail-header">
        <h4 style={{ color: colors.text, margin: 0 }}>{node.detail.title}</h4>
        <button
          className="pt-detail-close"
          onClick={onClose}
          aria-label="Close detail"
        >
          &times;
        </button>
      </div>

      {node.badges && node.badges.length > 0 && (
        <div className="pt-detail-badges">
          {node.badges.map((b, i) => {
            const bc = getNodeColors(theme, b.colorKey)
            return (
              <span
                key={i}
                className="pt-badge"
                style={{ background: bc.bg, color: bc.accent, borderColor: bc.accent }}
              >
                {b.text}
              </span>
            )
          })}
        </div>
      )}

      <div className="pt-detail-sections">
        {node.detail.sections.map((s, i) => (
          <div key={i} className="pt-detail-section">
            <div className="pt-detail-label">{s.label}</div>
            {Array.isArray(s.content) ? (
              <ul className="pt-detail-list">
                {s.content.map((item, j) => (
                  <li key={j}>{item}</li>
                ))}
              </ul>
            ) : (
              <div className="pt-detail-value">{s.content}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
