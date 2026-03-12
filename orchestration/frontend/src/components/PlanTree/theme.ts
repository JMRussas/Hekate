// Orchestration Engine - PlanTree Theme
//
// Configurable color map keyed to node function. Each node type gets
// a distinct accent color for its left border, icon, and badge.
// Includes HSL utilities for the runtime theme configurator.
//
// Depends on: (none)
// Used by:    PlanTree/index.tsx, PlanTree/PlanTreeNode.tsx,
//             PlanTree/hooks/usePlanTreeTheme.ts, PlanTree/ThemeConfigurator.tsx

export interface NodeColors {
  accent: string    // Left border, icon tint
  bg: string        // Node background (subtle)
  text: string      // Label color
}

export interface PlanTreeTheme {
  // Structural
  plan: NodeColors
  phase: NodeColors
  // Task types
  code: NodeColors
  research: NodeColors
  analysis: NodeColors
  asset: NodeColors
  integration: NodeColors
  documentation: NodeColors
  // Meta sections
  question: NodeColors
  risk: NodeColors
  test_strategy: NodeColors
  // Complexity
  simple: NodeColors
  medium: NodeColors
  complex: NodeColors
  // Connector lines
  connectorColor: string
  connectorWidth: number
  // Selection
  selectedBorder: string
  selectedBg: string
}

export const defaultTheme: PlanTreeTheme = {
  plan:          { accent: '#6c8cff', bg: '#1a2040', text: '#e1e4ed' },
  phase:         { accent: '#ab47bc', bg: '#2a1b3a', text: '#ce93d8' },

  code:          { accent: '#4fc3f7', bg: '#1b2a3a', text: '#b3e5fc' },
  research:      { accent: '#66bb6a', bg: '#1b3a2a', text: '#a5d6a7' },
  analysis:      { accent: '#ffa726', bg: '#3a2a1b', text: '#ffcc80' },
  asset:         { accent: '#ef5350', bg: '#3a1b1b', text: '#ef9a9a' },
  integration:   { accent: '#7e57c2', bg: '#2a1b3a', text: '#b39ddb' },
  documentation: { accent: '#78909c', bg: '#1b2a2a', text: '#b0bec5' },

  question:      { accent: '#ffb74d', bg: '#3a2a1b', text: '#ffe0b2' },
  risk:          { accent: '#e57373', bg: '#3a1b1b', text: '#ffcdd2' },
  test_strategy: { accent: '#81c784', bg: '#1b3a1b', text: '#c8e6c9' },

  simple:        { accent: '#66bb6a', bg: '#1b3a2a', text: '#a5d6a7' },
  medium:        { accent: '#ffa726', bg: '#3a2a1b', text: '#ffcc80' },
  complex:       { accent: '#ef5350', bg: '#3a1b1b', text: '#ef9a9a' },

  connectorColor: '#2a2d3a',
  connectorWidth: 2,

  selectedBorder: '#6c8cff',
  selectedBg: '#1a2040',
}

export const lightTheme: PlanTreeTheme = {
  plan:          { accent: '#4a6cf7', bg: '#e8edf8', text: '#1a1d27' },
  phase:         { accent: '#7b1fa2', bg: '#f3e5f5', text: '#4a148c' },

  code:          { accent: '#0277bd', bg: '#e1f5fe', text: '#01579b' },
  research:      { accent: '#2e7d32', bg: '#e8f5e9', text: '#1b5e20' },
  analysis:      { accent: '#e65100', bg: '#fff3e0', text: '#bf360c' },
  asset:         { accent: '#c62828', bg: '#ffebee', text: '#b71c1c' },
  integration:   { accent: '#4527a0', bg: '#ede7f6', text: '#311b92' },
  documentation: { accent: '#546e7a', bg: '#eceff1', text: '#37474f' },

  question:      { accent: '#ef6c00', bg: '#fff3e0', text: '#e65100' },
  risk:          { accent: '#c62828', bg: '#ffebee', text: '#b71c1c' },
  test_strategy: { accent: '#2e7d32', bg: '#e8f5e9', text: '#1b5e20' },

  simple:        { accent: '#2e7d32', bg: '#e8f5e9', text: '#1b5e20' },
  medium:        { accent: '#e65100', bg: '#fff3e0', text: '#bf360c' },
  complex:       { accent: '#c62828', bg: '#ffebee', text: '#b71c1c' },

  connectorColor: '#dde0e8',
  connectorWidth: 2,

  selectedBorder: '#4a6cf7',
  selectedBg: '#e8edf8',
}

export type NodeType = keyof Omit<PlanTreeTheme, 'connectorColor' | 'connectorWidth' | 'selectedBorder' | 'selectedBg'>

/** All node types grouped by category — used by ThemeConfigurator */
export const NODE_TYPE_GROUPS: { label: string; types: NodeType[] }[] = [
  { label: 'Structural', types: ['plan', 'phase'] },
  { label: 'Task Types', types: ['code', 'research', 'analysis', 'asset', 'integration', 'documentation'] },
  { label: 'Meta', types: ['question', 'risk', 'test_strategy'] },
  { label: 'Complexity', types: ['simple', 'medium', 'complex'] },
]

export function getNodeColors(theme: PlanTreeTheme, nodeType: NodeType): NodeColors {
  return theme[nodeType] ?? theme.plan
}

// ── HSL Utilities ──

/** Convert hex color (#rrggbb or #rgb) to [h, s, l] where h: 0-360, s/l: 0-100 */
export function hexToHsl(hex: string): [number, number, number] {
  const cleaned = hex.replace('#', '')
  const full = cleaned.length === 3
    ? cleaned[0] + cleaned[0] + cleaned[1] + cleaned[1] + cleaned[2] + cleaned[2]
    : cleaned
  const r = parseInt(full.substring(0, 2), 16) / 255
  const g = parseInt(full.substring(2, 4), 16) / 255
  const b = parseInt(full.substring(4, 6), 16) / 255

  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  if (max === min) return [0, 0, Math.round(l * 100)]

  const d = max - min
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
  let h = 0
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6
  else if (max === g) h = ((b - r) / d + 2) / 6
  else h = ((r - g) / d + 4) / 6

  return [Math.round(h * 360), Math.round(s * 100), Math.round(l * 100)]
}

/** Convert HSL values to hex string (#rrggbb). h: 0-360, s/l: 0-100 */
export function hslToHex(h: number, s: number, l: number): string {
  const sn = s / 100
  const ln = l / 100
  const c = (1 - Math.abs(2 * ln - 1)) * sn
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = ln - c / 2

  let r = 0, g = 0, b = 0
  if (h < 60)       { r = c; g = x }
  else if (h < 120) { r = x; g = c }
  else if (h < 180) { g = c; b = x }
  else if (h < 240) { g = x; b = c }
  else if (h < 300) { r = x; b = c }
  else              { r = c; b = x }

  const toHex = (v: number) => Math.round((v + m) * 255).toString(16).padStart(2, '0')
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`
}

/** Format HSL values as a CSS string */
export function hslToString(h: number, s: number, l: number): string {
  return `hsl(${h}, ${s}%, ${l}%)`
}
