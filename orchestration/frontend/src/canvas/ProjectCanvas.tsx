// Hekate canvas — ProjectCanvas component
//
// Owns the lifecycle (loading → loaded → error), the underlying Graph
// instance, and the SSE subscription. Substantive logic lives in the
// pure modules (planSource, flowMapping, layout, liveEvents); this file
// is the glue.
//
// Live updates flow as: subscribeEvents → applyLiveEvent(graph, ev) →
// bump version → re-derive flow nodes → React Flow rerenders. The Graph
// stays in a ref so mutations are O(1) and don't require copying state
// on every event.

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  ReactFlowProvider,
  type Edge as RFEdge,
  type Node as RFNode,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { CanvasNode } from './CanvasNode'
import type { SSEEvent } from '../types'
import { Graph } from './graph'
import { PlanSource, type PlanSourceDeps } from './planSource'
import { graphToFlow, type FlowNodeData } from './flowMapping'
import { applyLayout } from './layout'
import { applyLiveEvent } from './liveEvents'

/**
 * A subscription primitive — fires `onEvent` for every SSE event observed
 * on this project's stream. Returns an unsubscribe function. Kept as a
 * plain function (not a hook) so production wiring and tests can both
 * provide implementations.
 */
export type SubscribeEvents = (
  projectId: string,
  onEvent: (event: SSEEvent) => void,
) => () => void

export interface ProjectCanvasProps {
  projectId: string
  deps: PlanSourceDeps
  /** Optional. When omitted, the canvas renders the initial snapshot only. */
  subscribeEvents?: SubscribeEvents
}

type Phase =
  | { kind: 'loading' }
  | { kind: 'loaded' }
  | { kind: 'error'; message: string }

const NODE_TYPES: NodeTypes = { canvas: CanvasNode }

export function ProjectCanvas({ projectId, deps, subscribeEvents }: ProjectCanvasProps) {
  const graphRef = useRef<Graph>(new Graph())
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' })
  const [version, setVersion] = useState(0)

  // Initial load.
  useEffect(() => {
    let cancelled = false
    const graph = new Graph()
    graphRef.current = graph

    new PlanSource(projectId, deps)
      .load(graph)
      .then(() => {
        if (cancelled) return
        setPhase({ kind: 'loaded' })
        setVersion((v) => v + 1)
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

  // SSE → graph mutations.
  useEffect(() => {
    if (!subscribeEvents) return
    return subscribeEvents(projectId, (ev) => {
      applyLiveEvent(graphRef.current, ev)
      setVersion((v) => v + 1)
    })
  }, [projectId, subscribeEvents])

  // Derive React Flow data from the current graph.
  const flow = useMemo<{ nodes: RFNode<FlowNodeData>[]; edges: RFEdge[] }>(() => {
    const { nodes, edges } = graphToFlow(graphRef.current)
    return { nodes: applyLayout(nodes, edges).map(asCanvasNode), edges }
  }, [version])

  if (phase.kind === 'loading') {
    return <div role="status">Loading canvas…</div>
  }
  if (phase.kind === 'error') {
    return <div role="alert">Error: {phase.message}</div>
  }

  return (
    <ReactFlowProvider>
      <div style={{ width: '100%', height: '100%' }}>
        <ReactFlow
          nodes={flow.nodes}
          edges={flow.edges}
          nodeTypes={NODE_TYPES}
          fitView
        />
      </div>
    </ReactFlowProvider>
  )
}

/** React Flow uses `type` to pick a renderer; mark every node 'canvas'. */
function asCanvasNode(n: RFNode<FlowNodeData>): RFNode<FlowNodeData> {
  return { ...n, type: 'canvas' }
}
