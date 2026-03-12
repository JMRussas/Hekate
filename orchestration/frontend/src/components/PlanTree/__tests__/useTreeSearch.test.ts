// Orchestration Engine - useTreeSearch Tests
//
// Tests for the tree search hook: visibility filtering, match detection,
// case insensitivity, and multi-field matching.
//
// Depends on: hooks/useTreeSearch.ts, types.ts
// Used by:    (test suite)

import { renderHook, act } from '@testing-library/react'
import { useTreeSearch } from '../hooks/useTreeSearch'
import type { TreeNode } from '../types'

function makeNode(overrides: Partial<TreeNode> & { id: string }): TreeNode {
  return {
    type: 'code',
    label: overrides.label ?? overrides.id,
    children: [],
    ...overrides,
  }
}

// Tree structure:
//   root
//     ├── phase1 (label: "Setup Phase")
//     │     ├── task1 (label: "Build API", sublabel: "REST endpoints")
//     │     └── task2 (label: "Write Tests")
//     └── phase2 (label: "Deploy Phase")
//           └── task3 (label: "Configure CI", badges: [{text:"complex", colorKey:"complex"}])
const tree: TreeNode = makeNode({
  id: 'root',
  label: 'Project Root',
  children: [
    makeNode({
      id: 'phase1',
      label: 'Setup Phase',
      type: 'phase',
      children: [
        makeNode({ id: 'task1', label: 'Build API', sublabel: 'REST endpoints' }),
        makeNode({ id: 'task2', label: 'Write Tests' }),
      ],
    }),
    makeNode({
      id: 'phase2',
      label: 'Deploy Phase',
      type: 'phase',
      children: [
        makeNode({
          id: 'task3',
          label: 'Configure CI',
          badges: [{ text: 'complex', colorKey: 'complex' }],
        }),
      ],
    }),
  ],
})

describe('useTreeSearch', () => {
  it('returns all nodes visible and no matches when query is empty', () => {
    const { result } = renderHook(() => useTreeSearch(tree, ''))
    expect(result.current.visibleIds.size).toBe(6) // root + phase1 + task1 + task2 + phase2 + task3
    expect(result.current.matchIds.size).toBe(0)
    expect(result.current.matchCount).toBe(0)
  })

  it('makes matching leaf node and its ancestors visible', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'Build API'))
    expect(result.current.matchIds.has('task1')).toBe(true)
    expect(result.current.matchCount).toBe(1)
    // Ancestors should be visible but not match
    expect(result.current.visibleIds.has('root')).toBe(true)
    expect(result.current.visibleIds.has('phase1')).toBe(true)
    expect(result.current.visibleIds.has('task1')).toBe(true)
    // Unrelated branch should not be visible
    expect(result.current.visibleIds.has('phase2')).toBe(false)
    expect(result.current.visibleIds.has('task3')).toBe(false)
  })

  it('returns empty match set when nothing matches', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'nonexistent xyz'))
    expect(result.current.matchIds.size).toBe(0)
    expect(result.current.matchCount).toBe(0)
    expect(result.current.visibleIds.size).toBe(0)
  })

  it('performs case-insensitive matching', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'build api'))
    expect(result.current.matchIds.has('task1')).toBe(true)
    expect(result.current.matchCount).toBe(1)
  })

  it('matches against sublabel text', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'REST'))
    expect(result.current.matchIds.has('task1')).toBe(true)
    expect(result.current.matchCount).toBe(1)
  })

  it('matches against badge text', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'complex'))
    expect(result.current.matchIds.has('task3')).toBe(true)
    expect(result.current.matchCount).toBe(1)
  })

  it('matches multiple nodes with a common term', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'Phase'))
    // Both "Setup Phase" and "Deploy Phase" match
    expect(result.current.matchIds.has('phase1')).toBe(true)
    expect(result.current.matchIds.has('phase2')).toBe(true)
    expect(result.current.matchCount).toBe(2)
  })

  it('provides match navigation that wraps around', () => {
    const { result } = renderHook(() => useTreeSearch(tree, 'Phase'))
    expect(result.current.activeMatchIndex).toBe(0)

    act(() => result.current.nextMatch())
    expect(result.current.activeMatchIndex).toBe(1)

    act(() => result.current.nextMatch())
    expect(result.current.activeMatchIndex).toBe(0) // wraps

    act(() => result.current.prevMatch())
    expect(result.current.activeMatchIndex).toBe(1) // wraps backward
  })

  it('matches against detail section content', () => {
    const nodeWithDetail: TreeNode = makeNode({
      id: 'root',
      label: 'Root',
      children: [
        makeNode({
          id: 'detailed',
          label: 'Some Task',
          detail: {
            title: 'Task Detail',
            sections: [{ label: 'Notes', content: 'Uses PostgreSQL database' }],
          },
        }),
      ],
    })
    const { result } = renderHook(() => useTreeSearch(nodeWithDetail, 'PostgreSQL'))
    expect(result.current.matchIds.has('detailed')).toBe(true)
  })
})
