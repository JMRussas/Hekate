import { describe, it, expect } from 'vitest'
import type { Edge as RFEdge, Node as RFNode } from '@xyflow/react'
import { applyLayout } from './layout'
import type { FlowNodeData } from './flowMapping'

function makeNode(id: string): RFNode<FlowNodeData> {
  return {
    id,
    position: { x: 0, y: 0 },
    data: { label: id, kind: 'task', status: 'planned' },
  }
}

function makeEdge(from: string, to: string): RFEdge {
  return { id: `${from}->${to}`, source: from, target: to }
}

describe('applyLayout', () => {
  it('returns an empty array unchanged for an empty graph', () => {
    expect(applyLayout([], [])).toEqual([])
  })

  it('assigns a position to a single node', () => {
    const out = applyLayout([makeNode('a')], [])
    expect(out).toHaveLength(1)
    expect(out[0].position).toEqual(expect.objectContaining({
      x: expect.any(Number),
      y: expect.any(Number),
    }))
  })

  it('places target below source for a single edge (top-to-bottom)', () => {
    const nodes = [makeNode('a'), makeNode('b')]
    const edges = [makeEdge('a', 'b')]
    const out = applyLayout(nodes, edges)
    const a = out.find(n => n.id === 'a')!
    const b = out.find(n => n.id === 'b')!
    expect(b.position.y).toBeGreaterThan(a.position.y)
  })

  it('places fan-out siblings at the same depth', () => {
    const nodes = [makeNode('a'), makeNode('b'), makeNode('c')]
    const edges = [makeEdge('a', 'b'), makeEdge('a', 'c')]
    const out = applyLayout(nodes, edges)
    const b = out.find(n => n.id === 'b')!
    const c = out.find(n => n.id === 'c')!
    expect(b.position.y).toBe(c.position.y)
  })

  it('preserves node identity and data when positioning', () => {
    const input = makeNode('x')
    input.data.label = 'custom label'
    const [out] = applyLayout([input], [])
    expect(out.id).toBe('x')
    expect(out.data.label).toBe('custom label')
  })
})
