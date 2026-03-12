// Orchestration Engine - PlanTree Theme Hook
//
// Reactive theme management with localStorage persistence,
// MutationObserver for data-theme changes, and deep merge.
//
// Depends on: PlanTree/theme.ts
// Used by:    PlanTree/index.tsx

import { useState, useEffect, useCallback, useRef } from 'react'
import type { PlanTreeTheme, NodeType, NodeColors } from '../theme'
import { defaultTheme, lightTheme } from '../theme'

type ColorRole = 'accent' | 'bg' | 'text'

const STORAGE_KEY_DARK = 'plantree-theme-dark'
const STORAGE_KEY_LIGHT = 'plantree-theme-light'

type NodeOverrides = Partial<Record<NodeType, Partial<NodeColors>>>

function getStorageKey(isDark: boolean): string {
  return isDark ? STORAGE_KEY_DARK : STORAGE_KEY_LIGHT
}

function loadOverrides(isDark: boolean): NodeOverrides {
  try {
    const raw = localStorage.getItem(getStorageKey(isDark))
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function saveOverrides(isDark: boolean, overrides: NodeOverrides): void {
  const key = getStorageKey(isDark)
  if (Object.keys(overrides).length === 0) {
    localStorage.removeItem(key)
  } else {
    localStorage.setItem(key, JSON.stringify(overrides))
  }
}

function detectDark(): boolean {
  if (typeof document === 'undefined') return true
  return document.documentElement.getAttribute('data-theme') !== 'light'
}

/** Deep-merge node-level overrides onto a base theme */
function mergeTheme(base: PlanTreeTheme, overrides: NodeOverrides): PlanTreeTheme {
  const result = { ...base }
  for (const key of Object.keys(overrides) as NodeType[]) {
    const nodeOverride = overrides[key]
    if (nodeOverride) {
      result[key] = { ...base[key], ...nodeOverride }
    }
  }
  return result
}

export interface UsePlanTreeThemeResult {
  theme: PlanTreeTheme
  isDark: boolean
  setNodeColor: (nodeType: NodeType, role: ColorRole, hex: string) => void
  resetNode: (nodeType: NodeType) => void
  resetAll: () => void
}

export function usePlanTreeTheme(): UsePlanTreeThemeResult {
  const [isDark, setIsDark] = useState(detectDark)
  const [overrides, setOverrides] = useState<NodeOverrides>(() => loadOverrides(detectDark()))
  const isDarkRef = useRef(isDark)
  isDarkRef.current = isDark

  // Watch data-theme attribute changes
  useEffect(() => {
    if (typeof document === 'undefined') return

    const observer = new MutationObserver(() => {
      const dark = detectDark()
      setIsDark(dark)
      setOverrides(loadOverrides(dark))
    })

    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })

    return () => observer.disconnect()
  }, [])

  const setNodeColor = useCallback((nodeType: NodeType, role: ColorRole, hex: string) => {
    setOverrides(prev => {
      const next = { ...prev, [nodeType]: { ...prev[nodeType], [role]: hex } }
      saveOverrides(isDarkRef.current, next)
      return next
    })
  }, [])

  const resetNode = useCallback((nodeType: NodeType) => {
    setOverrides(prev => {
      const next = { ...prev }
      delete next[nodeType]
      saveOverrides(isDarkRef.current, next)
      return next
    })
  }, [])

  const resetAll = useCallback(() => {
    setOverrides({})
    saveOverrides(isDarkRef.current, {})
  }, [])

  const base = isDark ? defaultTheme : lightTheme
  const theme = mergeTheme(base, overrides)

  return { theme, isDark, setNodeColor, resetNode, resetAll }
}
