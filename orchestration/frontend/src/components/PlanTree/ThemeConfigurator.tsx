// Orchestration Engine - PlanTree Theme Configurator
//
// Compact popover for live HSL-based color editing of tree node types.
// Grouped by category, each node type shows 3 color swatches (accent, bg, text).
// Clicking a swatch reveals inline H/S/L sliders with spectrum backgrounds.
//
// Depends on: PlanTree/theme.ts, PlanTree/hooks/usePlanTreeTheme.ts
// Used by:    PlanTree/index.tsx

import { useState, useEffect, useCallback, useRef } from 'react'
import type { PlanTreeTheme, NodeType } from './theme'
import { NODE_TYPE_GROUPS, hexToHsl, hslToHex, getNodeColors } from './theme'

type ColorRole = 'accent' | 'bg' | 'text'
const ROLES: ColorRole[] = ['accent', 'bg', 'text']

interface Props {
  theme: PlanTreeTheme
  setNodeColor: (nodeType: NodeType, role: ColorRole, hex: string) => void
  resetNode: (nodeType: NodeType) => void
  resetAll: () => void
}

interface ActiveSwatch {
  nodeType: NodeType
  role: ColorRole
}

export default function ThemeConfigurator({ theme, setNodeColor, resetNode, resetAll }: Props) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState<ActiveSwatch | null>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)

  // Close on click-outside
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (popoverRef.current?.contains(target)) return
      if (btnRef.current?.contains(target)) return
      setOpen(false)
      setActive(null)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setOpen(false); setActive(null) }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  const togglePopover = useCallback(() => {
    setOpen(o => {
      if (o) setActive(null)
      return !o
    })
  }, [])

  const handleSwatchClick = useCallback((nodeType: NodeType, role: ColorRole) => {
    setActive(prev =>
      prev?.nodeType === nodeType && prev.role === role ? null : { nodeType, role }
    )
  }, [])

  return (
    <div style={{ position: 'relative' }}>
      <button
        ref={btnRef}
        className="pt-theme-btn"
        onClick={togglePopover}
        aria-label="Theme configurator"
        title="Customize node colors"
      >
        Palette
      </button>
      {open && (
        <div ref={popoverRef} className="pt-theme-popover">
          {NODE_TYPE_GROUPS.map(group => (
            <div key={group.label} className="pt-theme-group">
              <div className="pt-theme-group-label">{group.label}</div>
              {group.types.map(nt => (
                <NodeRow
                  key={nt}
                  nodeType={nt}
                  theme={theme}
                  active={active}
                  onSwatchClick={handleSwatchClick}
                  setNodeColor={setNodeColor}
                  resetNode={resetNode}
                />
              ))}
            </div>
          ))}
          <button className="pt-theme-reset" onClick={resetAll}>
            Reset All
          </button>
        </div>
      )}
    </div>
  )
}

// ── NodeRow: one node type with 3 swatches + optional sliders ──

interface NodeRowProps {
  nodeType: NodeType
  theme: PlanTreeTheme
  active: ActiveSwatch | null
  onSwatchClick: (nodeType: NodeType, role: ColorRole) => void
  setNodeColor: (nodeType: NodeType, role: ColorRole, hex: string) => void
  resetNode: (nodeType: NodeType) => void
}

function NodeRow({ nodeType, theme, active, onSwatchClick, setNodeColor, resetNode }: NodeRowProps) {
  const colors = getNodeColors(theme, nodeType)
  const isActive = active?.nodeType === nodeType
  const activeRole = isActive ? active.role : null
  const displayName = nodeType.replace('_', ' ')

  return (
    <div>
      <div className="pt-theme-row">
        <span className="pt-theme-name">{displayName}</span>
        {ROLES.map(role => (
          <div
            key={role}
            className={`pt-theme-swatch ${activeRole === role ? 'active' : ''}`}
            style={{ background: colors[role] }}
            onClick={() => onSwatchClick(nodeType, role)}
            title={`${role}: ${colors[role]}`}
          />
        ))}
        <button
          className="pt-theme-swatch-reset"
          onClick={() => resetNode(nodeType)}
          title="Reset this node"
          style={{
            background: 'none', border: 'none', cursor: 'pointer',
            fontSize: '0.625rem', color: 'var(--text-dim)', padding: '0 2px',
          }}
        >
          x
        </button>
      </div>
      {isActive && activeRole && (
        <HslSliders
          hex={colors[activeRole]}
          onChange={hex => setNodeColor(nodeType, activeRole, hex)}
        />
      )}
    </div>
  )
}

// ── HSL Sliders ──

interface HslSlidersProps {
  hex: string
  onChange: (hex: string) => void
}

function HslSliders({ hex, onChange }: HslSlidersProps) {
  const [h, s, l] = hexToHsl(hex)

  const handleH = (v: number) => onChange(hslToHex(v, s, l))
  const handleS = (v: number) => onChange(hslToHex(h, v, l))
  const handleL = (v: number) => onChange(hslToHex(h, s, v))

  // Gradient backgrounds for slider tracks
  const hueGrad = 'linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)'
  const satGrad = `linear-gradient(to right, ${hslToHex(h, 0, l)}, ${hslToHex(h, 100, l)})`
  const litGrad = `linear-gradient(to right, ${hslToHex(h, s, 0)}, ${hslToHex(h, s, 50)}, ${hslToHex(h, s, 100)})`

  return (
    <div className="pt-theme-sliders">
      <SliderRow label="H" value={h} max={360} gradient={hueGrad} onChange={handleH} />
      <SliderRow label="S" value={s} max={100} gradient={satGrad} onChange={handleS} />
      <SliderRow label="L" value={l} max={100} gradient={litGrad} onChange={handleL} />
    </div>
  )
}

interface SliderRowProps {
  label: string
  value: number
  max: number
  gradient: string
  onChange: (v: number) => void
}

function SliderRow({ label, value, max, gradient, onChange }: SliderRowProps) {
  return (
    <div className="pt-theme-slider-row">
      <span className="pt-theme-slider-label">{label}</span>
      <input
        type="range"
        className="pt-theme-slider"
        min={0}
        max={max}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{ background: gradient }}
      />
      <span className="pt-theme-slider-label" style={{ width: '24px', textAlign: 'right' }}>
        {value}
      </span>
    </div>
  )
}
