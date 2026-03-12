// Orchestration Engine - useTreeKeyboard Tests
//
// Tests WAI-ARIA Treeview keyboard navigation: arrow keys,
// Home/End, Enter/Space selection, expand/collapse, and
// visibility-based node skipping.
//
// Depends on: ../hooks/useTreeKeyboard.ts, ../hooks/useExpandState.ts, ../types.ts
// Used by:    (tests only)

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useTreeKeyboard } from '../hooks/useTreeKeyboard'
import { useExpandState } from '../hooks/useExpandState'
import type { TreeNode } from '../types'

// ── Test fixtures ──

function makeNode(id: string, children: TreeNode[] = [], opts: Partial<TreeNode> = {}): TreeNode {
  return {
    id,
    type: 'code',
    label: `Node ${id}`,
    children,
    badges: [],
    ...opts,
  }
}

// Tree structure:
//   A (expanded by default)
//     A1 (leaf)
//     A2 (has children, explicitly collapsed)
//       A2a (leaf)
//   B (leaf)
function buildTestTree(): TreeNode[] {
  const a2a = makeNode('A2a')
  const a2 = makeNode('A2', [a2a], { defaultExpanded: false })
  const a1 = makeNode('A1')
  const a = makeNode('A', [a1, a2], { defaultExpanded: true })
  const b = makeNode('B')
  return [a, b]
}

// Helper to create a keyboard event object for the hook
function keyEvent(key: string): React.KeyboardEvent {
  return {
    key,
    preventDefault: vi.fn(),
  } as unknown as React.KeyboardEvent
}

// ── Tests ──

describe('useTreeKeyboard', () => {
  let roots: TreeNode[]
  let onSelect: ReturnType<typeof vi.fn>
  let onDeselect: ReturnType<typeof vi.fn>

  beforeEach(() => {
    roots = buildTestTree()
    onSelect = vi.fn()
    onDeselect = vi.fn()
    // Mock querySelector for scrollIntoView
    vi.spyOn(document, 'querySelector').mockReturnValue(null)
  })

  function renderBoth() {
    // Render both hooks together so expandState is shared
    return renderHook(() => {
      const expandState = useExpandState(roots)
      const keyboard = useTreeKeyboard(roots, expandState, onSelect, onDeselect)
      return { expandState, keyboard }
    })
  }

  it('initializes focusedId to the first root node', () => {
    const { result } = renderBoth()
    expect(result.current.keyboard.focusedId).toBe('A')
  })

  it('Arrow Down moves focus to the next visible node', () => {
    const { result } = renderBoth()

    // A is expanded (depth 0 < 2), so visible order is: A, A1, A2, B
    // A2 is collapsed (depth 1 < 2 means expanded, but A2's children at depth 2 are not auto-expanded)
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('A1')

    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('A2')

    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('B')
  })

  it('Arrow Down at the last node is a no-op', () => {
    const { result } = renderBoth()

    // Navigate to B (last visible)
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('End')))
    expect(result.current.keyboard.focusedId).toBe('B')

    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('B')
  })

  it('Arrow Up moves focus to the previous visible node', () => {
    const { result } = renderBoth()

    // Move to A1 first
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('A1')

    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowUp')))
    expect(result.current.keyboard.focusedId).toBe('A')
  })

  it('Arrow Up at the first node is a no-op', () => {
    const { result } = renderBoth()
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowUp')))
    expect(result.current.keyboard.focusedId).toBe('A')
  })

  it('Arrow Right expands a collapsed node with children', () => {
    const { result } = renderBoth()

    // Navigate to A2 (collapsed)
    act(() => result.current.keyboard.setFocusedId('A2'))
    expect(result.current.expandState.isExpanded('A2')).toBe(false)

    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowRight')))
    expect(result.current.expandState.isExpanded('A2')).toBe(true)
  })

  it('Arrow Right on expanded node moves to first child', () => {
    const { result } = renderBoth()

    // A is expanded, ArrowRight should move to A1
    expect(result.current.expandState.isExpanded('A')).toBe(true)
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowRight')))
    expect(result.current.keyboard.focusedId).toBe('A1')
  })

  it('Arrow Right on a leaf node is a no-op', () => {
    const { result } = renderBoth()

    act(() => result.current.keyboard.setFocusedId('A1'))
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowRight')))
    expect(result.current.keyboard.focusedId).toBe('A1')
  })

  it('Arrow Left collapses an expanded node', () => {
    const { result } = renderBoth()

    // A is expanded
    expect(result.current.expandState.isExpanded('A')).toBe(true)
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowLeft')))
    expect(result.current.expandState.isExpanded('A')).toBe(false)
  })

  it('Arrow Left on collapsed node moves to parent', () => {
    const { result } = renderBoth()

    // Navigate to A1 (child of A)
    act(() => result.current.keyboard.setFocusedId('A1'))
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowLeft')))
    expect(result.current.keyboard.focusedId).toBe('A')
  })

  it('Home jumps to the first visible node', () => {
    const { result } = renderBoth()

    act(() => result.current.keyboard.setFocusedId('B'))
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('Home')))
    expect(result.current.keyboard.focusedId).toBe('A')
  })

  it('End jumps to the last visible node', () => {
    const { result } = renderBoth()
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('End')))
    expect(result.current.keyboard.focusedId).toBe('B')
  })

  it('Enter triggers onSelect with the focused node', () => {
    const { result } = renderBoth()
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('Enter')))
    expect(onSelect).toHaveBeenCalledWith(roots[0])
  })

  it('Space triggers onSelect with the focused node', () => {
    const { result } = renderBoth()
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent(' ')))
    expect(onSelect).toHaveBeenCalledWith(roots[0])
  })

  it('Escape triggers onDeselect', () => {
    const { result } = renderBoth()
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('Escape')))
    expect(onDeselect).toHaveBeenCalled()
  })

  it('nodes with collapsed parents are skipped in navigation', () => {
    const { result } = renderBoth()

    // Collapse A so its children are hidden
    act(() => result.current.expandState.collapse('A'))

    // Visible order is now: A, B
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('B')
  })

  it('* expands all siblings at the current level', () => {
    const { result } = renderBoth()

    // Collapse A first
    act(() => result.current.expandState.collapse('A'))
    expect(result.current.expandState.isExpanded('A')).toBe(false)

    // Press * — A and B are siblings. A has children, B does not.
    // A should be expanded.
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('*')))
    expect(result.current.expandState.isExpanded('A')).toBe(true)
  })

  it('expanding a node reveals its children for navigation', () => {
    const { result } = renderBoth()

    // A2 is collapsed by default (depth >= 2). Navigate to it and expand.
    act(() => result.current.keyboard.setFocusedId('A2'))
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowRight')))
    expect(result.current.expandState.isExpanded('A2')).toBe(true)

    // Now ArrowDown from A2 should go to A2a
    act(() => result.current.keyboard.handleTreeKeyDown(keyEvent('ArrowDown')))
    expect(result.current.keyboard.focusedId).toBe('A2a')
  })

  it('visibleNodes list reflects expand state', () => {
    const { result } = renderBoth()

    // Default: A expanded, A2 collapsed
    const ids = result.current.keyboard.visibleNodes.map(v => v.id)
    expect(ids).toEqual(['A', 'A1', 'A2', 'B'])

    // Expand A2
    act(() => result.current.expandState.expand('A2'))
    const ids2 = result.current.keyboard.visibleNodes.map(v => v.id)
    expect(ids2).toEqual(['A', 'A1', 'A2', 'A2a', 'B'])
  })

  it('preventDefault is called on handled keys', () => {
    const { result } = renderBoth()
    const event = keyEvent('ArrowDown')
    act(() => result.current.keyboard.handleTreeKeyDown(event))
    expect(event.preventDefault).toHaveBeenCalled()
  })
})
