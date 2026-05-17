// Hekate canvas — ProjectCanvas component
//
// Thin React wrapper that wires PlanSource → Graph → React Flow. The
// substantive logic lives in planSource.ts (data) and flowMapping.ts
// (projection); this file is the glue that owns the lifecycle states
// (loading / loaded / error) and hands the result to <ReactFlow/>.
//
// Auto-layout is intentionally deferred — every node currently positions
// at (0, 0). A later cycle will wire dagre / elk for layered placement.

import { useEffect, useState } from 'react'
import {
  ReactFlow,
  ReactFlowProvider,
  type Edge as RFEdge,
  type Node as RFNode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { Graph } from './graph'
import { PlanSource, type PlanSourceDeps } from './planSource'
import { graphToFlow, type FlowNodeData } from './flowMapping'
import { applyLayout } from './layout'

export interface ProjectCanvasProps {
  projectId: string
  deps: PlanSourceDeps
}

type Phase =
  | { kind: 'loading' }
  | { kind: 'loaded'; nodes: RFNode<FlowNodeData>[]; edges: RFEdge[] }
  | { kind: 'error'; message: string }

export function ProjectCanvas({ projectId, deps }: ProjectCanvasProps) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    const source = new PlanSource(projectId, deps)
    const graph = new Graph()

    source
      .load(graph)
      .then(() => {
        if (cancelled) return
        const { nodes, edges } = graphToFlow(graph)
        const positioned = applyLayout(nodes, edges)
        setPhase({ kind: 'loaded', nodes: positioned, edges })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof Error ? err.message : String(err)
        setPhase({ kind: 'error', message })
      })

    return () => {
      cancelled = true
    }
  }, [projectId, deps])

  if (phase.kind === 'loading') {
    return <div role="status">Loading canvas…</div>
  }
  if (phase.kind === 'error') {
    return <div role="alert">Error: {phase.message}</div>
  }

  return (
    <ReactFlowProvider>
      <div style={{ width: '100%', height: '100%' }}>
        <ReactFlow nodes={phase.nodes} edges={phase.edges} fitView />
      </div>
    </ReactFlowProvider>
  )
}
