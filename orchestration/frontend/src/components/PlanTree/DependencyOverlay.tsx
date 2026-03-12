// Orchestration Engine - PlanTree Dependency Overlay
//
// SVG overlay that draws bezier curves between dependent task nodes.
// Recalculates on expand/collapse, resize, and scroll.
//
// Depends on: DependencyContext.tsx
// Used by:    PlanTree/index.tsx

import { useEffect, useRef, useState, useCallback } from 'react'
import { useDependencyContext } from './DependencyContext'

interface PathData {
  id: string
  from: string
  to: string
  d: string
  color: string
}

export default function DependencyOverlay({ containerRef }: { containerRef: React.RefObject<HTMLDivElement | null> }) {
  const { nodeRefs, dependencyMap, downstreamMap, hoveredNodeId, setHoveredNodeId } = useDependencyContext()
  const [paths, setPaths] = useState<PathData[]>([])
  const svgRef = useRef<SVGSVGElement>(null)
  const rafRef = useRef<number>(0)

  const recalculate = useCallback(() => {
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(() => {
      const container = containerRef.current
      if (!container) return

      const containerRect = container.getBoundingClientRect()
      const newPaths: PathData[] = []

      dependencyMap.forEach((deps, nodeId) => {
        const targetEl = nodeRefs.current.get(nodeId)
        if (!targetEl) return

        for (const depId of deps) {
          const sourceEl = nodeRefs.current.get(depId)
          if (!sourceEl) continue

          const sRect = sourceEl.getBoundingClientRect()
          const tRect = targetEl.getBoundingClientRect()

          // Source: right-center of source node
          const x1 = sRect.right - containerRect.left
          const y1 = sRect.top + sRect.height / 2 - containerRect.top

          // Target: left edge of target node
          const x2 = tRect.left - containerRect.left
          const y2 = tRect.top + tRect.height / 2 - containerRect.top

          // Cubic bezier control points
          const dx = Math.abs(x2 - x1)
          const cp = Math.min(50, dx * 0.4)

          const d = `M ${x1} ${y1} C ${x1 + cp} ${y1}, ${x2 - cp} ${y2}, ${x2} ${y2}`

          // Get accent color from source node's computed style
          const color = getComputedStyle(sourceEl).borderLeftColor || 'var(--accent)'

          newPaths.push({
            id: `${depId}->${nodeId}`,
            from: depId,
            to: nodeId,
            d,
            color,
          })
        }
      })

      setPaths(newPaths)
    })
  }, [containerRef, dependencyMap, nodeRefs])

  // Recalculate on mount and when dependencies change
  useEffect(() => {
    recalculate()

    // ResizeObserver on container
    const container = containerRef.current
    if (!container) return

    const ro = new ResizeObserver(recalculate)
    ro.observe(container)

    // Also recalculate on window scroll/resize
    window.addEventListener('resize', recalculate)
    window.addEventListener('scroll', recalculate, true)

    return () => {
      ro.disconnect()
      window.removeEventListener('resize', recalculate)
      window.removeEventListener('scroll', recalculate, true)
      cancelAnimationFrame(rafRef.current)
    }
  }, [recalculate])

  // Hover changes only affect path styling (opacity, stroke-width, dash),
  // not path positions — no recalculation needed.

  if (paths.length === 0) return null

  // Determine which paths to highlight
  const highlightedFromIds = new Set<string>()
  const highlightedToIds = new Set<string>()
  if (hoveredNodeId) {
    // Upstream: what the hovered node depends on
    const upstream = dependencyMap.get(hoveredNodeId) ?? []
    for (const depId of upstream) {
      highlightedFromIds.add(`${depId}->${hoveredNodeId}`)
    }
    // Downstream: what depends on the hovered node (pre-computed)
    const downstream = downstreamMap.get(hoveredNodeId) ?? []
    for (const nodeId of downstream) {
      highlightedToIds.add(`${hoveredNodeId}->${nodeId}`)
    }
  }

  const hasHighlight = hoveredNodeId != null

  return (
    <svg
      ref={svgRef}
      className="pt-dep-overlay"
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'visible' }}
    >
      {paths.map(p => {
        const isUpstream = highlightedFromIds.has(p.id)
        const isDownstream = highlightedToIds.has(p.id)
        const isHighlighted = isUpstream || isDownstream
        const dimmed = hasHighlight && !isHighlighted

        return (
          <path
            key={p.id}
            d={p.d}
            fill="none"
            stroke={p.color}
            strokeWidth={isHighlighted ? 2 : 1.5}
            strokeDasharray={isDownstream ? '6 3' : 'none'}
            opacity={dimmed ? 0.12 : isHighlighted ? 0.9 : 0.3}
            className="pt-dep-line"
            style={{ pointerEvents: 'auto' }}
            onMouseEnter={() => setHoveredNodeId(p.to)}
            onMouseLeave={() => setHoveredNodeId(null)}
          />
        )
      })}
    </svg>
  )
}
