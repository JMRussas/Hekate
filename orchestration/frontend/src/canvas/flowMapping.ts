// Hekate canvas — Graph → React Flow projection
//
// Pure function that turns our Graph into React Flow's node/edge shape.
// Kept separate from the component so the projection logic is unit-testable
// without spinning up jsdom or the React Flow renderer.

import type { Edge as RFEdge, Node as RFNode } from '@xyflow/react'
import { Graph, type Node as CNode } from './graph'

export interface FlowNodeData extends Record<string, unknown> {
  label: string
  kind: CNode['kind']
  status: NonNullable<CNode['status']>
  result?: string
  error?: string
}

export interface FlowResult {
  nodes: RFNode<FlowNodeData>[]
  edges: RFEdge[]
}

export function graphToFlow(graph: Graph): FlowResult {
  const nodes: RFNode<FlowNodeData>[] = []
  for (const [id, n] of graph.nodes) {
    const node: RFNode<FlowNodeData> = {
      id,
      position: { x: 0, y: 0 }, // React Flow requires a position; auto-layout is a later concern
      data: {
        label: n.intent || id,
        kind: n.kind,
        status: n.status ?? 'planned',
        ...(n.result !== undefined ? { result: n.result } : {}),
        ...(n.error !== undefined ? { error: n.error } : {}),
      },
    }
    if (n.parentId !== undefined) {
      node.parentId = n.parentId
      // 'parent' constrains children to render within the parent's bounds —
      // this is what gives us the "containment as visible nesting" property.
      node.extent = 'parent'
    }
    nodes.push(node)
  }

  const edges: RFEdge[] = graph.edges.map((e, i) => ({
    id: `${e.fromId}->${e.toId}#${e.fromPort}#${i}`,
    source: e.fromId,
    target: e.toId,
    sourceHandle: e.fromPort,
  }))

  return { nodes, edges }
}
