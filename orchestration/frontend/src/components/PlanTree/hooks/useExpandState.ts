// Orchestration Engine - PlanTree Expand State Hook
//
// Centralized expand/collapse state for the tree. Provides a Map<id, boolean>
// and toggle/expandAll/collapseAll operations. Replaces per-node local state
// so the keyboard hook can compute visible nodes.
//
// Depends on: ../types.ts
// Used by:    ../index.tsx, useTreeKeyboard.ts

import { useState, useCallback, useMemo, useEffect } from 'react'
import type { TreeNode } from '../types'

/** Walk tree and collect initial expanded state based on defaultExpanded / depth */
function computeDefaults(node: TreeNode, depth: number, out: Map<string, boolean>): void {
  const hasChildren = node.children.length > 0
  if (hasChildren) {
    out.set(node.id, node.defaultExpanded ?? depth < 2)
  }
  for (const child of node.children) {
    computeDefaults(child, depth + 1, out)
  }
}

export interface ExpandState {
  expandedMap: Map<string, boolean>
  isExpanded: (id: string) => boolean
  toggle: (id: string) => void
  expand: (id: string) => void
  collapse: (id: string) => void
  expandAll: () => void
  collapseAll: () => void
}

export function useExpandState(roots: TreeNode[]): ExpandState {
  const defaultMap = useMemo(() => {
    const m = new Map<string, boolean>()
    for (const root of roots) {
      computeDefaults(root, 0, m)
    }
    return m
  }, [roots])

  const [expandedMap, setExpandedMap] = useState<Map<string, boolean>>(defaultMap)

  // Re-sync when tree changes: add new node defaults, remove stale entries
  useEffect(() => {
    setExpandedMap(prev => {
      const next = new Map<string, boolean>()
      for (const [id, defaultVal] of defaultMap) {
        next.set(id, prev.get(id) ?? defaultVal)
      }
      return next
    })
  }, [defaultMap])

  const isExpanded = useCallback(
    (id: string) => expandedMap.get(id) ?? false,
    [expandedMap],
  )

  const toggle = useCallback((id: string) => {
    setExpandedMap(prev => {
      const next = new Map(prev)
      next.set(id, !prev.get(id))
      return next
    })
  }, [])

  const expand = useCallback((id: string) => {
    setExpandedMap(prev => {
      if (prev.get(id) === true) return prev
      const next = new Map(prev)
      next.set(id, true)
      return next
    })
  }, [])

  const collapse = useCallback((id: string) => {
    setExpandedMap(prev => {
      if (prev.get(id) === false) return prev
      const next = new Map(prev)
      next.set(id, false)
      return next
    })
  }, [])

  const expandAll = useCallback(() => {
    setExpandedMap(prev => {
      const next = new Map(prev)
      for (const key of next.keys()) next.set(key, true)
      return next
    })
  }, [])

  const collapseAll = useCallback(() => {
    setExpandedMap(prev => {
      const next = new Map(prev)
      for (const key of next.keys()) next.set(key, false)
      return next
    })
  }, [])

  return { expandedMap, isExpanded, toggle, expand, collapse, expandAll, collapseAll }
}
