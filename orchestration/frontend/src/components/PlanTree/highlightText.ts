// Orchestration Engine - Text Highlight Utility
//
// Splits text by case-insensitive query match and wraps matching
// segments in <mark> elements for search result highlighting.
//
// Depends on: (none)
// Used by:    PlanTree/PlanTreeNode.tsx

import { createElement } from 'react'
import type { ReactNode } from 'react'

/** Escape special regex characters in a string. */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Highlight occurrences of `query` in `text` by wrapping them in
 * <mark className="pt-highlight"> elements. Returns original text
 * if query is empty.
 */
export function highlightText(text: string, query: string): ReactNode {
  if (!query) return text

  const escaped = escapeRegex(query)
  const regex = new RegExp(`(${escaped})`, 'gi')
  const parts = text.split(regex)

  if (parts.length === 1) return text

  const testRegex = new RegExp(`^${escaped}$`, 'i')
  return parts.map((part, i) =>
    testRegex.test(part)
      ? createElement('mark', { className: 'pt-highlight', key: i }, part)
      : part
  )
}
