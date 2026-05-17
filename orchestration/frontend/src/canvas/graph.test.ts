import { describe, it, expect } from 'vitest'
import { Graph } from './graph'

describe('Graph', () => {
  it('starts with no nodes', () => {
    const g = new Graph()
    expect(g.nodes.size).toBe(0)
  })

  it('addNode stores the node by id', () => {
    const g = new Graph()
    g.addNode({ id: 'n1', kind: 'tool_call', config: {}, intent: 'do a thing' })
    expect(g.nodes.has('n1')).toBe(true)
    expect(g.nodes.get('n1')?.intent).toBe('do a thing')
  })

  it('connect adds a flow edge with default port "out"', () => {
    const g = new Graph()
    g.addNode({ id: 'a', kind: 'tool_call', config: {}, intent: '' })
    g.addNode({ id: 'b', kind: 'transform', config: {}, intent: '' })
    g.connect('a', 'b')
    expect(g.edges).toHaveLength(1)
    expect(g.edges[0]).toEqual({ fromId: 'a', toId: 'b', fromPort: 'out' })
  })

  it('connect supports typed ports', () => {
    const g = new Graph()
    g.addNode({ id: 'check', kind: 'check', config: {}, intent: '' })
    g.addNode({ id: 'ok', kind: 'tool_call', config: {}, intent: '' })
    g.addNode({ id: 'recover', kind: 'sub_agent', config: {}, intent: '' })
    g.connect('check', 'ok', 'pass')
    g.connect('check', 'recover', 'fail')
    expect(g.edges).toEqual([
      { fromId: 'check', toId: 'ok', fromPort: 'pass' },
      { fromId: 'check', toId: 'recover', fromPort: 'fail' },
    ])
  })

  it('connect throws on unknown source or target', () => {
    const g = new Graph()
    g.addNode({ id: 'a', kind: 'tool_call', config: {}, intent: '' })
    expect(() => g.connect('a', 'nope')).toThrow(/unknown.*nope/)
    expect(() => g.connect('nope', 'a')).toThrow(/unknown.*nope/)
  })

  it('addNode stores parentId for containment', () => {
    const g = new Graph()
    g.addNode({ id: 'p', kind: 'sub_agent', config: {}, intent: '' })
    g.addNode({ id: 'c', kind: 'tool_call', config: {}, intent: '', parentId: 'p' })
    expect(g.nodes.get('c')?.parentId).toBe('p')
  })

  it('addNode throws if parentId is unknown', () => {
    const g = new Graph()
    expect(() =>
      g.addNode({ id: 'c', kind: 'tool_call', config: {}, intent: '', parentId: 'nope' })
    ).toThrow(/unknown.*nope/)
  })

  it('containment queries: contained, depth, containmentRoots', () => {
    const g = new Graph()
    g.addNode({ id: 'plan', kind: 'sub_agent', config: {}, intent: '' })
    g.addNode({ id: 'p1', kind: 'sub_agent', config: {}, intent: '', parentId: 'plan' })
    g.addNode({ id: 't1', kind: 'tool_call', config: {}, intent: '', parentId: 'p1' })
    g.addNode({ id: 't2', kind: 'tool_call', config: {}, intent: '', parentId: 'p1' })

    expect(g.containmentRoots()).toEqual(['plan'])
    expect(g.contained('plan')).toEqual(['p1'])
    expect(g.contained('p1')).toEqual(['t1', 't2'])
    expect(g.depth('plan')).toBe(0)
    expect(g.depth('p1')).toBe(1)
    expect(g.depth('t1')).toBe(2)
  })

  it('a new node has status "planned" by default', () => {
    const g = new Graph()
    g.addNode({ id: 'n1', kind: 'tool_call', config: {}, intent: '' })
    expect(g.nodes.get('n1')?.status).toBe('planned')
  })

  it('setStatus mutates status and records optional result/error', () => {
    const g = new Graph()
    g.addNode({ id: 'n1', kind: 'tool_call', config: {}, intent: '' })

    g.setStatus('n1', 'running')
    expect(g.nodes.get('n1')?.status).toBe('running')

    g.setStatus('n1', 'done', { result: 'output text' })
    expect(g.nodes.get('n1')?.status).toBe('done')
    expect(g.nodes.get('n1')?.result).toBe('output text')

    g.setStatus('n1', 'failed', { error: 'boom' })
    expect(g.nodes.get('n1')?.status).toBe('failed')
    expect(g.nodes.get('n1')?.error).toBe('boom')
  })
})
