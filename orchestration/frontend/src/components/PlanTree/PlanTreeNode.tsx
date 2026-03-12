// Orchestration Engine - PlanTreeNode Component
//
// Recursive tree node with expand/collapse, connector lines,
// color-coded left border, badges, and WAI-ARIA treeview roles.
// Uses centralized expand state and roving tabindex.
//
// Depends on: types.ts, theme.ts, DependencyContext.tsx, highlightText.ts, hooks/useExpandState.ts
// Used by:    PlanTree/index.tsx

import { useRef, useCallback } from 'react'
import type { TreeNode } from './types'
import type { PlanTreeTheme } from './theme'
import { getNodeColors } from './theme'
import { useRegisterNode, useDependencyContext } from './DependencyContext'
import { highlightText } from './highlightText'
import type { ExpandState } from './hooks/useExpandState'

interface Props {
  node: TreeNode
  theme: PlanTreeTheme
  depth: number
  isLast: boolean
  selectedId: string | null
  focusedId: string | null
  onSelect: (node: TreeNode) => void
  expandState: ExpandState
  siblingCount: number
  positionIndex: number
  visibleIds?: Set<string>
  matchIds?: Set<string>
  searchQuery?: string
}

export default function PlanTreeNode({
  node, theme, depth, isLast, selectedId, focusedId,
  onSelect, expandState, siblingCount, positionIndex,
  visibleIds, matchIds, searchQuery,
}: Props) {
  // All hooks must be called unconditionally (Rules of Hooks)
  const nodeRef = useRef<HTMLDivElement>(null)
  const registerRef = useRegisterNode(node.id)
  const { setHoveredNodeId } = useDependencyContext()
  const hasChildren = node.children.length > 0

  const setNodeRef = useCallback((el: HTMLDivElement | null) => {
    (nodeRef as React.MutableRefObject<HTMLDivElement | null>).current = el
    registerRef(el)
  }, [registerRef])

  const handleMouseEnter = useCallback(() => {
    if (node.dependsOn?.length) setHoveredNodeId(node.id)
  }, [node.id, node.dependsOn, setHoveredNodeId])

  const handleMouseLeave = useCallback(() => {
    setHoveredNodeId(null)
  }, [setHoveredNodeId])

  const toggle = useCallback(() => {
    if (hasChildren) expandState.toggle(node.id)
  }, [hasChildren, expandState, node.id])

  const handleClick = useCallback(() => {
    if (node.detail) onSelect(node)
    else toggle()
  }, [node, onSelect, toggle])

  // Search filtering: hide nodes not in visibleIds (after all hooks)
  if (visibleIds && !visibleIds.has(node.id)) return null

  const isDimmed = matchIds && !matchIds.has(node.id)
  const expanded = hasChildren && expandState.isExpanded(node.id)
  const isSelected = selectedId === node.id
  const isFocused = focusedId === node.id
  const colors = getNodeColors(theme, node.type)

  // Chevron icon
  const chevron = hasChildren
    ? (expanded ? '▾' : '▸')
    : '·'

  return (
    <div className={`pt-node-wrapper ${isDimmed ? 'pt-node-dimmed' : ''}`} data-last={isLast}>
      {/* Connector lines */}
      {depth > 0 && (
        <div
          className="pt-connector"
          style={{
            borderColor: theme.connectorColor,
            borderWidth: theme.connectorWidth,
          }}
        />
      )}

      {/* Node card */}
      <div
        ref={setNodeRef}
        className={`pt-node ${isSelected ? 'pt-node-selected' : ''}`}
        style={{
          borderLeftColor: colors.accent,
          background: isSelected ? theme.selectedBg : colors.bg,
        }}
        data-node-id={node.id}
        onClick={handleClick}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        tabIndex={isFocused ? 0 : -1}
        role="treeitem"
        aria-expanded={hasChildren ? expanded : undefined}
        aria-selected={isSelected}
        aria-level={depth + 1}
        aria-setsize={siblingCount}
        aria-posinset={positionIndex + 1}
      >
        <div className="pt-node-header">
          {/* Expand/collapse toggle */}
          {hasChildren ? (
            <button
              className="pt-toggle"
              onClick={e => { e.stopPropagation(); toggle() }}
              aria-hidden="true"
              tabIndex={-1}
              style={{ color: colors.accent }}
            >
              {chevron}
            </button>
          ) : (
            <span className="pt-toggle pt-leaf" aria-hidden="true" style={{ color: theme.connectorColor }}>{chevron}</span>
          )}

          {/* Label */}
          <div className="pt-label-group">
            <span className="pt-label" style={{ color: colors.text }}>{searchQuery ? highlightText(node.label, searchQuery) : node.label}</span>
            {node.badges && node.badges.length > 0 && (
              <span className="pt-badges" aria-hidden="true">
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
              </span>
            )}
          </div>

          {/* Detail indicator */}
          {node.detail && (
            <span className="pt-detail-arrow" aria-hidden="true" style={{ color: colors.accent }}>
              ›
            </span>
          )}
        </div>

        {/* Sublabel (truncated) */}
        {node.sublabel && (
          <div className="pt-sublabel">{searchQuery ? highlightText(node.sublabel, searchQuery) : node.sublabel}</div>
        )}
      </div>

      {/* Children — always rendered, CSS grid animates height */}
      {hasChildren && (
        <div className={`pt-children ${expanded ? 'pt-expanded' : ''}`} role="group">
          <div className="pt-children-inner">
            {node.children.map((child, i) => (
              <PlanTreeNode
                key={child.id}
                node={child}
                theme={theme}
                depth={depth + 1}
                isLast={i === node.children.length - 1}
                selectedId={selectedId}
                focusedId={focusedId}
                onSelect={onSelect}
                expandState={expandState}
                siblingCount={node.children.length}
                positionIndex={i}
                visibleIds={visibleIds}
                matchIds={matchIds}
                searchQuery={searchQuery}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
