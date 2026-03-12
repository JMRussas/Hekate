// Orchestration Engine - PlanTree Keyboard Navigation Hook
//
// Implements WAI-ARIA Treeview keyboard navigation pattern with
// roving tabindex. Computes visible nodes from expand state and
// handles Arrow, Home, End, Enter, Space, Escape, and * keys.
//
// Depends on: ../types.ts, useExpandState.ts
// Used by:    ../index.tsx

import { useState, useCallback, useRef, useEffect } from 'react'
import type { TreeNode } from '../types'
import type { ExpandState } from './useExpandState'

export interface VisibleNode {
  id: string
  node: TreeNode
  depth: number
  parentId: string | null
  siblings: TreeNode[]
  index: number  // position among siblings
}

/** Depth-first traversal that skips children of collapsed nodes */
function computeVisibleNodes(
  nodes: TreeNode[],
  expandState: ExpandState,
  depth: number,
  parentId: string | null,
  out: VisibleNode[],
): void {
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i]
    out.push({ id: node.id, node, depth, parentId, siblings: nodes, index: i })
    if (node.children.length > 0 && expandState.isExpanded(node.id)) {
      computeVisibleNodes(node.children, expandState, depth + 1, node.id, out)
    }
  }
}

export interface UseTreeKeyboardResult {
  focusedId: string | null
  setFocusedId: (id: string | null) => void
  handleTreeKeyDown: (e: React.KeyboardEvent) => void
  visibleNodes: VisibleNode[]
}

export function useTreeKeyboard(
  roots: TreeNode[],
  expandState: ExpandState,
  onSelect: (node: TreeNode) => void,
  onDeselect: () => void,
): UseTreeKeyboardResult {
  const [focusedId, setFocusedId] = useState<string | null>(
    roots.length > 0 ? roots[0].id : null,
  )

  // Keep a ref to avoid stale closures in the keydown handler
  const focusedIdRef = useRef(focusedId)
  useEffect(() => { focusedIdRef.current = focusedId }, [focusedId])

  // Compute visible nodes (memoized by identity — recalculated each render)
  const visibleNodes: VisibleNode[] = []
  computeVisibleNodes(roots, expandState, 0, null, visibleNodes)

  const visibleRef = useRef(visibleNodes)
  visibleRef.current = visibleNodes

  const findIndex = useCallback(() => {
    const id = focusedIdRef.current
    return visibleRef.current.findIndex(v => v.id === id)
  }, [])

  const focusNode = useCallback((id: string) => {
    setFocusedId(id)
    // Scroll the node into view
    const el = document.querySelector(`[data-node-id="${CSS.escape(id)}"]`) as HTMLElement | null
    el?.scrollIntoView({ block: 'nearest' })
  }, [])

  const handleTreeKeyDown = useCallback((e: React.KeyboardEvent) => {
    const visible = visibleRef.current
    if (visible.length === 0) return

    const idx = findIndex()
    const current = idx >= 0 ? visible[idx] : null

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        if (idx < visible.length - 1) focusNode(visible[idx + 1].id)
        break

      case 'ArrowUp':
        e.preventDefault()
        if (idx > 0) focusNode(visible[idx - 1].id)
        break

      case 'ArrowRight':
        e.preventDefault()
        handleArrowRight(current, expandState, visible, idx, focusNode)
        break

      case 'ArrowLeft':
        e.preventDefault()
        handleArrowLeft(current, expandState, focusNode)
        break

      case 'Home':
        e.preventDefault()
        if (visible.length > 0) focusNode(visible[0].id)
        break

      case 'End':
        e.preventDefault()
        if (visible.length > 0) focusNode(visible[visible.length - 1].id)
        break

      case 'Enter':
      case ' ':
        e.preventDefault()
        if (current) onSelect(current.node)
        break

      case 'Escape':
        e.preventDefault()
        onDeselect()
        break

      case '*':
        e.preventDefault()
        handleExpandSiblings(current, expandState)
        break
    }
  }, [expandState, onSelect, onDeselect, findIndex, focusNode])

  return { focusedId, setFocusedId, handleTreeKeyDown, visibleNodes }
}

/** ArrowRight: expand collapsed, move to first child if expanded, no-op on leaf */
function handleArrowRight(
  current: VisibleNode | null,
  expandState: ExpandState,
  visible: VisibleNode[],
  idx: number,
  focusNode: (id: string) => void,
): void {
  if (!current) return
  const hasChildren = current.node.children.length > 0
  if (!hasChildren) return

  if (!expandState.isExpanded(current.id)) {
    expandState.expand(current.id)
  } else if (idx < visible.length - 1) {
    // Move to first child (next visible node at deeper depth)
    focusNode(visible[idx + 1].id)
  }
}

/** ArrowLeft: collapse expanded, move to parent if collapsed/leaf */
function handleArrowLeft(
  current: VisibleNode | null,
  expandState: ExpandState,
  focusNode: (id: string) => void,
): void {
  if (!current) return
  const hasChildren = current.node.children.length > 0

  if (hasChildren && expandState.isExpanded(current.id)) {
    expandState.collapse(current.id)
  } else if (current.parentId) {
    focusNode(current.parentId)
  }
}

/** * key: expand all siblings at the current level */
function handleExpandSiblings(
  current: VisibleNode | null,
  expandState: ExpandState,
): void {
  if (!current) return
  for (const sibling of current.siblings) {
    if (sibling.children.length > 0) {
      expandState.expand(sibling.id)
    }
  }
}
