// Orchestration Engine - PlanTree Component
//
// Hybrid tree/node visualization for plan data. Renders plan structure
// as a navigable tree with color-coded nodes, connector lines, and
// a slide-in detail panel. Supports configurable themes.
//
// Depends on: types.ts, theme.ts, buildTree.ts, PlanTreeNode.tsx, NodeDetail.tsx,
//             SearchBar.tsx, hooks/useTreeSearch.ts, hooks/usePlanTreeTheme.ts,
//             ThemeConfigurator.tsx
// Used by:    pages/ProjectDetail.tsx

import { useState, useMemo, useCallback, useRef } from 'react'
import './PlanTree.css'
import type { PlanData } from '../../types'
import { buildPlanTree } from './buildTree'
import type { TreeNode } from './types'
import PlanTreeNode from './PlanTreeNode'
import NodeDetail from './NodeDetail'
import { DependencyProvider } from './DependencyContext'
import DependencyOverlay from './DependencyOverlay'
import { usePlanTreeTheme } from './hooks/usePlanTreeTheme'
import { useExpandState } from './hooks/useExpandState'
import { useTreeKeyboard } from './hooks/useTreeKeyboard'
import { useTreeSearch } from './hooks/useTreeSearch'
import ThemeConfigurator from './ThemeConfigurator'
import SearchBar from './SearchBar'

interface Props {
  plan: PlanData
}

export default function PlanTree({ plan }: Props) {
  const { theme, setNodeColor, resetNode, resetAll } = usePlanTreeTheme()
  const { tree, dependencyMap } = useMemo(() => buildPlanTree(plan), [plan])
  const [selectedNode, setSelectedNode] = useState<TreeNode | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const lastFocusedNodeRef = useRef<string | null>(null)

  const expandState = useExpandState(tree.children)

  const handleSelect = useCallback((node: TreeNode) => {
    setSelectedNode(prev => prev?.id === node.id ? null : node)
  }, [])

  const handleClose = useCallback(() => {
    setSelectedNode(null)
    // Return focus to the node that opened the detail panel
    if (lastFocusedNodeRef.current) {
      const el = document.querySelector(
        `[data-node-id="${CSS.escape(lastFocusedNodeRef.current)}"]`,
      ) as HTMLElement | null
      el?.focus()
    }
  }, [])

  const { focusedId, handleTreeKeyDown } = useTreeKeyboard(
    tree.children, expandState, handleSelect, handleClose,
  )

  // Track which node had focus before detail panel opens
  if (focusedId) lastFocusedNodeRef.current = focusedId

  const { visibleIds, matchIds, matchCount, activeMatchIndex, nextMatch, prevMatch } =
    useTreeSearch(tree, searchQuery)

  const hasSearch = searchQuery.length > 0

  return (
    <DependencyProvider dependencyMap={dependencyMap}>
      <div ref={containerRef} className={`pt-container ${selectedNode ? 'pt-has-detail' : ''}`}>
        <div className="pt-toolbar">
          <SearchBar
            query={searchQuery}
            onChange={setSearchQuery}
            matchCount={matchCount}
            activeMatchIndex={activeMatchIndex}
            onNext={nextMatch}
            onPrev={prevMatch}
          />
          <button className="pt-toolbar-btn" onClick={expandState.expandAll} aria-label="Expand all nodes">
            Expand All
          </button>
          <button className="pt-toolbar-btn" onClick={expandState.collapseAll} aria-label="Collapse all nodes">
            Collapse All
          </button>
          <ThemeConfigurator
            theme={theme}
            setNodeColor={setNodeColor}
            resetNode={resetNode}
            resetAll={resetAll}
          />
        </div>

        <div
          className="pt-tree"
          role="tree"
          aria-label="Project Plan"
          onKeyDown={handleTreeKeyDown}
        >
          {tree.children.map((child, i) => (
            <PlanTreeNode
              key={child.id}
              node={child}
              theme={theme}
              depth={0}
              isLast={i === tree.children.length - 1}
              selectedId={selectedNode?.id ?? null}
              focusedId={focusedId}
              onSelect={handleSelect}
              expandState={expandState}
              siblingCount={tree.children.length}
              positionIndex={i}
              visibleIds={hasSearch ? visibleIds : undefined}
              matchIds={hasSearch ? matchIds : undefined}
              searchQuery={hasSearch ? searchQuery : undefined}
            />
          ))}
        </div>

        <DependencyOverlay containerRef={containerRef} />

        <NodeDetail
          node={selectedNode}
          theme={theme}
          onClose={handleClose}
        />
      </div>
    </DependencyProvider>
  )
}

// Re-export theme types for consumers
export type { PlanTreeTheme } from './theme'
export { defaultTheme, lightTheme } from './theme'
