import { describe, it, expect } from 'vitest'
import { Graph } from './graph'
import { graphToFlow } from './flowMapping'

describe('graphToFlow', () => {
  it('returns an empty result for an empty graph', () => {
    const { nodes, edges } = graphToFlow(new Graph())
    expect(nodes).toEqual([])
    expect(edges).toEqual([])
  })

  it('maps each Graph node to a React Flow node with id and data', () => {
    const g = new Graph()
    g.addNode({ id: 'n1', kind: 'task', config: { tier: 'sonnet' }, intent: 'do it' })
    g.addNode({ id: 'n2', kind: 'check', config: {}, intent: 'verify' })
    const { nodes } = graphToFlow(g)
    expect(nodes).toHaveLength(2)
    expect(nodes.map(n => n.id).sort()).toEqual(['n1', 'n2'])
    const n1 = nodes.find(n => n.id === 'n1')!
    expect(n1.data.label).toBe('do it')
    expect(n1.data.kind).toBe('task')
    expect(n1.data.status).toBe('planned')
  })

  it('maps each Graph edge to a React Flow edge with stable id', () => {
    const g = new Graph()
    g.addNode({ id: 'a', kind: 'task', config: {}, intent: '' })
    g.addNode({ id: 'b', kind: 'task', config: {}, intent: '' })
    g.connect('a', 'b', 'result')
    const { edges } = graphToFlow(g)
    expect(edges).toHaveLength(1)
    expect(edges[0]).toMatchObject({
      source: 'a',
      target: 'b',
      sourceHandle: 'result',
    })
    expect(edges[0].id).toBeTruthy()
  })

  it('emits containment as React Flow parent references with extent: parent', () => {
    const g = new Graph()
    g.addNode({ id: 'plan', kind: 'plan', config: {}, intent: 'the plan' })
    g.addNode({ id: 't1', kind: 'task', config: {}, intent: 't1', parentId: 'plan' })
    const { nodes } = graphToFlow(g)
    const plan = nodes.find(n => n.id === 'plan')!
    const t1 = nodes.find(n => n.id === 't1')!
    expect(plan.parentId).toBeUndefined()
    expect(t1.parentId).toBe('plan')
    expect(t1.extent).toBe('parent')
  })
})
