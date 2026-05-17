// Hekate canvas — auto-layout via dagre.
//
// Pure function that consumes React Flow nodes/edges (with placeholder
// positions) and returns the same nodes with computed x/y. Kept separate
// from flowMapping so layout can be swapped (elk, custom, manual hints)
// without touching the projection logic.
//
// Layout direction is top-to-bottom: roots at the top, dependents below.
// This matches the prototype's "plan → phase → task" reading order and
// keeps `target.y > source.y` invariants that the tests assert on.

import dagre from 'dagre'
import type { Edge as RFEdge, Node as RFNode } from '@xyflow/react'
import type { FlowNodeData } from './flowMapping'

// Default node footprint when dagre measures spacing. React Flow doesn't
// know real sizes until after first render; using a reasonable default
// keeps the initial layout sensible.
const NODE_WIDTH = 200
const NODE_HEIGHT = 80

export interface LayoutOptions {
  direction?: 'TB' | 'BT' | 'LR' | 'RL'
  /** Min horizontal spacing between nodes in the same rank. */
  nodeSep?: number
  /** Min vertical spacing between successive ranks. */
  rankSep?: number
}

export function applyLayout(
  nodes: RFNode<FlowNodeData>[],
  edges: RFEdge[],
  options: LayoutOptions = {},
): RFNode<FlowNodeData>[] {
  if (nodes.length === 0) return []

  const g = new dagre.graphlib.Graph()
  g.setGraph({
    rankdir: options.direction ?? 'TB',
    nodesep: options.nodeSep ?? 40,
    ranksep: options.rankSep ?? 60,
  })
  g.setDefaultEdgeLabel(() => ({}))

  for (const n of nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }
  for (const e of edges) {
    g.setEdge(e.source, e.target)
  }

  dagre.layout(g)

  // dagre returns center coordinates; React Flow expects top-left corner.
  return nodes.map((n) => {
    const placed = g.node(n.id)
    return {
      ...n,
      position: {
        x: placed.x - NODE_WIDTH / 2,
        y: placed.y - NODE_HEIGHT / 2,
      },
    }
  })
}
