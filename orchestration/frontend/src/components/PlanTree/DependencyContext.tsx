// Orchestration Engine - PlanTree Dependency Context
//
// Shared state for dependency visualization: node DOM refs,
// dependency graph, and hover tracking.
//
// Depends on: (none)
// Used by:    PlanTree/index.tsx, PlanTree/PlanTreeNode.tsx, PlanTree/DependencyOverlay.tsx

import { createContext, useContext, useCallback, useRef, useState, useMemo } from 'react'

/** Build reverse map: nodeId → list of nodes that depend on it */
function buildDownstreamMap(depMap: Map<string, string[]>): Map<string, string[]> {
  const downstream = new Map<string, string[]>()
  depMap.forEach((deps, nodeId) => {
    for (const depId of deps) {
      let list = downstream.get(depId)
      if (!list) {
        list = []
        downstream.set(depId, list)
      }
      list.push(nodeId)
    }
  })
  return downstream
}

interface DependencyContextValue {
  nodeRefs: React.MutableRefObject<Map<string, HTMLElement>>
  dependencyMap: Map<string, string[]>
  downstreamMap: Map<string, string[]>
  hoveredNodeId: string | null
  setHoveredNodeId: (id: string | null) => void
}

const DependencyCtx = createContext<DependencyContextValue | null>(null)

export function DependencyProvider({
  dependencyMap,
  children,
}: {
  dependencyMap: Map<string, string[]>
  children: React.ReactNode
}) {
  const nodeRefs = useRef(new Map<string, HTMLElement>())
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null)
  const downstreamMap = useMemo(() => buildDownstreamMap(dependencyMap), [dependencyMap])

  return (
    <DependencyCtx.Provider value={{ nodeRefs, dependencyMap, downstreamMap, hoveredNodeId, setHoveredNodeId }}>
      {children}
    </DependencyCtx.Provider>
  )
}

export function useDependencyContext(): DependencyContextValue {
  const ctx = useContext(DependencyCtx)
  if (!ctx) throw new Error('useDependencyContext must be used within DependencyProvider')
  return ctx
}

export function useRegisterNode(id: string) {
  const { nodeRefs } = useDependencyContext()

  return useCallback((el: HTMLElement | null) => {
    if (el) {
      nodeRefs.current.set(id, el)
    } else {
      nodeRefs.current.delete(id)
    }
  }, [id, nodeRefs])
}

export function useNodeDependencies(id: string) {
  const { dependencyMap, downstreamMap } = useDependencyContext()

  const upstream = dependencyMap.get(id) ?? []
  const downstream = downstreamMap.get(id) ?? []

  return { upstream, downstream }
}
