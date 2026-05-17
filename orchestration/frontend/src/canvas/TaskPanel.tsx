// Hekate canvas — TaskPanel slide-in.
//
// Presentational component that renders the full detail of a single
// selected task. Reads from the Task object directly (which we stash in
// the graph node's config during PlanSource projection), so no extra API
// call is needed when a user clicks a node.
//
// Actions (approve / retry / edit) are deliberately not in this commit —
// they're their own TDD cycle. This first cut is read-only so the
// "click a node, see what failed and why" loop works end to end.

import type { Task } from '../types'

export interface TaskPanelProps {
  task: Task
  onClose: () => void
}

export function TaskPanel({ task, onClose }: TaskPanelProps) {
  return (
    <aside
      role="complementary"
      aria-label="Task detail"
      data-testid="task-panel"
      style={{
        position: 'absolute',
        top: 0,
        right: 0,
        width: 420,
        height: '100%',
        background: '#161616',
        color: '#eee',
        borderLeft: '1px solid #333',
        boxShadow: '-4px 0 12px rgba(0,0,0,0.4)',
        padding: 16,
        overflowY: 'auto',
        zIndex: 10,
      }}
    >
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>{task.title}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close detail panel"
          style={{
            background: 'transparent',
            color: '#888',
            border: '1px solid #444',
            borderRadius: 4,
            padding: '2px 8px',
            cursor: 'pointer',
            fontSize: 12,
          }}
        >
          ✕ close
        </button>
      </header>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontSize: 11, marginBottom: 12, opacity: 0.8 }}>
        <span data-testid="meta-status">{task.status}</span>
        <span>·</span>
        <span data-testid="meta-tier">{task.model_tier}</span>
        <span>·</span>
        <span data-testid="meta-wave">wave {task.wave}</span>
        <span>·</span>
        <span data-testid="meta-task-type">{task.task_type}</span>
      </div>

      {task.description ? (
        <section style={{ marginBottom: 12, fontSize: 13, lineHeight: 1.4, opacity: 0.9 }}>
          {task.description}
        </section>
      ) : null}

      {task.tools.length > 0 ? (
        <section style={{ marginBottom: 12 }}>
          <h3 style={{ fontSize: 11, textTransform: 'uppercase', opacity: 0.6, margin: '0 0 6px' }}>
            Tools
          </h3>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {task.tools.map((t) => (
              <span
                key={t}
                style={{
                  background: '#2a2a2a',
                  padding: '2px 6px',
                  borderRadius: 3,
                  fontSize: 11,
                }}
              >
                {t}
              </span>
            ))}
          </div>
        </section>
      ) : null}

      {task.output_text ? (
        <section style={{ marginBottom: 12 }}>
          <h3 style={{ fontSize: 11, textTransform: 'uppercase', opacity: 0.6, margin: '0 0 6px' }}>
            Output
          </h3>
          <pre style={{
            background: '#0d0d0d',
            border: '1px solid #2a2a2a',
            padding: 8,
            borderRadius: 4,
            fontSize: 11,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            margin: 0,
            maxHeight: 300,
            overflowY: 'auto',
          }}>{task.output_text}</pre>
        </section>
      ) : null}

      {task.error ? (
        <section style={{ marginBottom: 12 }}>
          <h3 style={{ fontSize: 11, textTransform: 'uppercase', opacity: 0.6, margin: '0 0 6px', color: '#ff8888' }}>
            Error
          </h3>
          <pre style={{
            background: '#2a0d0d',
            border: '1px solid #6a2222',
            color: '#ffaaaa',
            padding: 8,
            borderRadius: 4,
            fontSize: 11,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            margin: 0,
            maxHeight: 200,
            overflowY: 'auto',
          }}>{task.error}</pre>
        </section>
      ) : null}
    </aside>
  )
}
