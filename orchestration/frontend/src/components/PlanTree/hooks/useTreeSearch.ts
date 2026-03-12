// Orchestration Engine - Tree Search Hook
//
// Filters tree nodes by search query, tracking which nodes match
// and which ancestors should remain visible. Provides match navigation.
//
// Depends on: types.ts
// Used by:    PlanTree/index.tsx

import { useState, useMemo, useCallback } from 'react'
import type { TreeNode } from '../types'

interface TreeSearchResult {
  visibleIds: Set<string>
  matchIds: Set<string>
  matchCount: number
  activeMatchIndex: number
  setActiveMatchIndex: (index: number) => void
  nextMatch: () => void
  prevMatch: () => void
}

/** Check if a single node's own content matches the query (case-insensitive). */
function nodeMatchesQuery(node: TreeNode, lowerQuery: string): boolean {
  if (node.label.toLowerCase().includes(lowerQuery)) return true
  if (node.sublabel?.toLowerCase().includes(lowerQuery)) return true
  if (node.badges?.some(b => b.text.toLowerCase().includes(lowerQuery))) return true
  if (node.detail) {
    for (const section of node.detail.sections) {
      const content = section.content
      if (typeof content === 'string') {
        if (content.toLowerCase().includes(lowerQuery)) return true
      } else {
        if (content.some(line => line.toLowerCase().includes(lowerQuery))) return true
      }
    }
  }
  return false
}

/**
 * Recursively collect matchIds and visibleIds for a subtree.
 * Returns true if this node or any descendant matches.
 */
function collectMatches(
  node: TreeNode,
  lowerQuery: string,
  matchIds: Set<string>,
  visibleIds: Set<string>,
): boolean {
  const selfMatches = nodeMatchesQuery(node, lowerQuery)
  let anyChildMatches = false

  for (const child of node.children) {
    if (collectMatches(child, lowerQuery, matchIds, visibleIds)) {
      anyChildMatches = true
    }
  }

  if (selfMatches) {
    matchIds.add(node.id)
    visibleIds.add(node.id)
    return true
  }

  if (anyChildMatches) {
    visibleIds.add(node.id)
    return true
  }

  return false
}

/** Collect all node IDs in a tree (for empty-query case). */
function collectAllIds(node: TreeNode, out: Set<string>): void {
  out.add(node.id)
  for (const child of node.children) {
    collectAllIds(child, out)
  }
}

export function useTreeSearch(tree: TreeNode, query: string): TreeSearchResult {
  const [activeMatchIndex, setActiveMatchIndex] = useState(0)

  const { visibleIds, matchIds } = useMemo(() => {
    const vis = new Set<string>()
    const mat = new Set<string>()

    if (!query.trim()) {
      collectAllIds(tree, vis)
      return { visibleIds: vis, matchIds: mat }
    }

    const lowerQuery = query.toLowerCase()
    collectMatches(tree, lowerQuery, mat, vis)
    return { visibleIds: vis, matchIds: mat }
  }, [tree, query])

  const matchCount = matchIds.size

  // Clamp activeMatchIndex when matchCount changes
  const clampedIndex = matchCount === 0 ? 0 : Math.min(activeMatchIndex, matchCount - 1)

  const nextMatch = useCallback(() => {
    if (matchCount === 0) return
    setActiveMatchIndex(prev => (prev + 1) % matchCount)
  }, [matchCount])

  const prevMatch = useCallback(() => {
    if (matchCount === 0) return
    setActiveMatchIndex(prev => (prev - 1 + matchCount) % matchCount)
  }, [matchCount])

  return {
    visibleIds,
    matchIds,
    matchCount,
    activeMatchIndex: clampedIndex,
    setActiveMatchIndex,
    nextMatch,
    prevMatch,
  }
}
