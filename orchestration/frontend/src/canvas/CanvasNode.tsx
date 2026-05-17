// Hekate canvas — node renderer.
//
// Renders a single graph node: label, kind, status badge, and (when
// present) result/error excerpts. The status badge is the load-bearing
// visual cue — it's what changes as SSE events arrive, so the "watching
// execution unfold" experience hinges on this widget.
//
// Styling is intentionally crude inline CSS for now — once the rest of
// the canvas lands we'll factor into the rest of the dashboard's styling
// system.

import { Handle, Position, type NodeProps } from '@xyflow/react'
import type { FlowNodeData } from './flowMapping'
import type { NodeStatus } from './graph'

const STATUS_COLOR: Record<NodeStatus, string> = {
  planned: '#888',
  ready: '#d4a800',
  running: '#0aa6ff',
  done: '#2ea043',
  failed: '#cf222e',
}

export function CanvasNode({ data }: NodeProps & { data: FlowNodeData }) {
  const color = STATUS_COLOR[data.status]
  return (
    <div
      data-testid={`canvas-node`}
      data-status={data.status}
      style={{
        border: `2px solid ${color}`,
        borderRadius: 6,
        padding: '8px 10px',
        background: '#1c1c1c',
        color: '#eee',
        minWidth: 180,
        fontSize: 12,
      }}
    >
      <Handle type="target" position={Position.Top} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <span
          aria-label="status"
          style={{
            background: color,
            color: '#000',
            fontWeight: 'bold',
            padding: '1px 6px',
            borderRadius: 3,
            fontSize: 10,
            textTransform: 'uppercase',
          }}
        >
          {data.status}
        </span>
        <span style={{ opacity: 0.6, fontSize: 10 }}>{data.kind}</span>
      </div>
      <div style={{ fontWeight: 500 }}>{data.label}</div>
      {data.result ? (
        <div style={{ marginTop: 4, fontSize: 11, opacity: 0.7 }}>→ {truncate(data.result)}</div>
      ) : null}
      {data.error ? (
        <div style={{ marginTop: 4, fontSize: 11, color: '#ff8888' }}>✗ {truncate(data.error)}</div>
      ) : null}
      <Handle type="source" position={Position.Bottom} />
    </div>
  )
}

function truncate(s: string, n = 80): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…'
}
