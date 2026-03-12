// Orchestration Engine - PlanTree Theme Tests
//
// Tests for HSL conversion utilities and usePlanTreeTheme hook.
//
// Depends on: PlanTree/theme.ts, PlanTree/hooks/usePlanTreeTheme.ts
// Used by:    (test suite only)

import { describe, it, expect, beforeEach } from 'vitest'
import { hexToHsl, hslToHex, hslToString, NODE_TYPE_GROUPS } from '../theme'

describe('hexToHsl', () => {
  it('converts pure red', () => {
    expect(hexToHsl('#ff0000')).toEqual([0, 100, 50])
  })

  it('converts pure green', () => {
    expect(hexToHsl('#00ff00')).toEqual([120, 100, 50])
  })

  it('converts pure blue', () => {
    expect(hexToHsl('#0000ff')).toEqual([240, 100, 50])
  })

  it('converts white', () => {
    expect(hexToHsl('#ffffff')).toEqual([0, 0, 100])
  })

  it('converts black', () => {
    expect(hexToHsl('#000000')).toEqual([0, 0, 0])
  })

  it('handles 3-digit hex shorthand', () => {
    expect(hexToHsl('#f00')).toEqual([0, 100, 50])
  })

  it('converts a mid-range color', () => {
    // #4080c0 → roughly h=210, s=50, l=50
    const [h, s, l] = hexToHsl('#4080c0')
    expect(h).toBe(210)
    expect(s).toBe(50)
    expect(l).toBe(50)
  })
})

describe('hslToHex', () => {
  it('converts pure red', () => {
    expect(hslToHex(0, 100, 50)).toBe('#ff0000')
  })

  it('converts pure green', () => {
    expect(hslToHex(120, 100, 50)).toBe('#00ff00')
  })

  it('converts pure blue', () => {
    expect(hslToHex(240, 100, 50)).toBe('#0000ff')
  })

  it('converts white', () => {
    expect(hslToHex(0, 0, 100)).toBe('#ffffff')
  })

  it('converts black', () => {
    expect(hslToHex(0, 0, 0)).toBe('#000000')
  })
})

describe('hexToHsl / hslToHex round-trip', () => {
  const testCases = ['#ff0000', '#00ff00', '#0000ff', '#ffffff', '#000000']

  testCases.forEach(hex => {
    it(`round-trips ${hex}`, () => {
      const [h, s, l] = hexToHsl(hex)
      expect(hslToHex(h, s, l)).toBe(hex)
    })
  })

  it('round-trips arbitrary colors within 1 step tolerance', () => {
    // Due to integer rounding in HSL, some colors may be off by 1
    const hex = '#5b9bd5'
    const [h, s, l] = hexToHsl(hex)
    const result = hslToHex(h, s, l)
    // Compare each channel — allow +-2 due to HSL rounding
    const parseChannel = (c: string, i: number) => parseInt(c.substring(1 + i * 2, 3 + i * 2), 16)
    for (let i = 0; i < 3; i++) {
      expect(Math.abs(parseChannel(hex, i) - parseChannel(result, i))).toBeLessThanOrEqual(2)
    }
  })
})

describe('hslToString', () => {
  it('formats correctly', () => {
    expect(hslToString(220, 80, 70)).toBe('hsl(220, 80%, 70%)')
  })

  it('formats zero values', () => {
    expect(hslToString(0, 0, 0)).toBe('hsl(0, 0%, 0%)')
  })
})

describe('NODE_TYPE_GROUPS', () => {
  it('has 4 groups', () => {
    expect(NODE_TYPE_GROUPS).toHaveLength(4)
  })

  it('covers all expected node types', () => {
    const allTypes = NODE_TYPE_GROUPS.flatMap(g => g.types)
    expect(allTypes).toContain('plan')
    expect(allTypes).toContain('phase')
    expect(allTypes).toContain('code')
    expect(allTypes).toContain('risk')
    expect(allTypes).toContain('simple')
    expect(allTypes).toContain('complex')
    expect(allTypes).toHaveLength(14)
  })
})

describe('usePlanTreeTheme localStorage', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('stores and retrieves overrides from localStorage', async () => {
    const { renderHook, act } = await import('@testing-library/react')
    const { usePlanTreeTheme } = await import('../hooks/usePlanTreeTheme')

    const { result } = renderHook(() => usePlanTreeTheme())

    act(() => {
      result.current.setNodeColor('plan', 'accent', '#ff0000')
    })

    expect(result.current.theme.plan.accent).toBe('#ff0000')
    // Check localStorage has the value
    const stored = JSON.parse(localStorage.getItem('plantree-theme-dark') || '{}')
    expect(stored.plan.accent).toBe('#ff0000')
  })

  it('resetAll clears all overrides', async () => {
    const { renderHook, act } = await import('@testing-library/react')
    const { usePlanTreeTheme } = await import('../hooks/usePlanTreeTheme')

    const { result } = renderHook(() => usePlanTreeTheme())

    act(() => {
      result.current.setNodeColor('plan', 'accent', '#ff0000')
      result.current.setNodeColor('phase', 'bg', '#00ff00')
    })

    expect(result.current.theme.plan.accent).toBe('#ff0000')

    act(() => {
      result.current.resetAll()
    })

    // Should be back to default
    expect(result.current.theme.plan.accent).not.toBe('#ff0000')
    expect(localStorage.getItem('plantree-theme-dark')).toBeNull()
  })

  it('resetNode clears a single node override', async () => {
    const { renderHook, act } = await import('@testing-library/react')
    const { usePlanTreeTheme } = await import('../hooks/usePlanTreeTheme')

    const { result } = renderHook(() => usePlanTreeTheme())

    act(() => {
      result.current.setNodeColor('plan', 'accent', '#ff0000')
      result.current.setNodeColor('phase', 'bg', '#00ff00')
    })

    act(() => {
      result.current.resetNode('plan')
    })

    // plan should be back to default, phase should still be overridden
    expect(result.current.theme.plan.accent).not.toBe('#ff0000')
    expect(result.current.theme.phase.bg).toBe('#00ff00')
  })
})
