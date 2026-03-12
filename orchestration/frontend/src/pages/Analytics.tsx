// Orchestration Engine - Analytics Page
//
// Admin-only analytics: cost breakdown, task outcomes, efficiency.
//
// Depends on: api/analytics.ts
// Used by:    App.tsx

import { useEffect, useState } from 'react'
import {
  getCostBreakdown, getTaskOutcomes, getEfficiency,
  type CostBreakdown, type TaskOutcomes, type Efficiency,
} from '../api/analytics'

const pctBar = (value: number, className = 'ok') => (
  <div className="progress-bar" role="progressbar"
    aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}
    style={{ width: '100px', display: 'inline-block', verticalAlign: 'middle' }}>
    <div className={`progress-fill ${className}`} style={{ width: `${Math.min(value * 100, 100)}%` }} />
  </div>
)

const pctClass = (rate: number) => rate >= 0.8 ? 'ok' : rate >= 0.5 ? 'warn' : 'danger'

export default function Analytics() {
  const [cost, setCost] = useState<CostBreakdown | null>(null)
  const [outcomes, setOutcomes] = useState<TaskOutcomes | null>(null)
  const [efficiency, setEfficiency] = useState<Efficiency | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const errors: string[] = []
    Promise.allSettled([
      getCostBreakdown().then(setCost),
      getTaskOutcomes().then(setOutcomes),
      getEfficiency().then(setEfficiency),
    ]).then(results => {
      for (const r of results) {
        if (r.status === 'rejected') errors.push(String(r.reason))
      }
      if (errors.length) setError(errors.join('; '))
      setLoading(false)
    })
  }, [])

  if (loading) return <p className="text-dim">Loading analytics...</p>

  return (
    <>
      <h2 className="mb-2">Analytics</h2>

      {error && <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>Failed to load: {error}</div>}

      {/* Cost Breakdown */}
      {cost && (
        <>
          <h3 className="mb-1">Cost Breakdown</h3>
          <div className="grid grid-2 mb-2">
            <div className="card">
              <h4>By Model Tier</h4>
              {cost.by_model_tier.length === 0 ? (
                <span className="text-dim">No data yet</span>
              ) : (
                <table>
                  <thead><tr><th>Tier</th><th>Tasks</th><th>Total Cost</th><th>Avg/Task</th></tr></thead>
                  <tbody>
                    {cost.by_model_tier.map(t => (
                      <tr key={t.model_tier}>
                        <td><span className={`badge ${t.model_tier}`}>{t.model_tier}</span></td>
                        <td>{t.task_count}</td>
                        <td className="cost">${t.cost_usd.toFixed(4)}</td>
                        <td className="text-dim">${t.avg_cost_per_task.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="card">
              <h4>By Project (top 10)</h4>
              {cost.by_project.length === 0 ? (
                <span className="text-dim">No data yet</span>
              ) : (
                <table>
                  <thead><tr><th>Project</th><th>Tasks</th><th>Cost</th></tr></thead>
                  <tbody>
                    {cost.by_project.slice(0, 10).map(p => (
                      <tr key={p.project_id}>
                        <td>{p.project_name}</td>
                        <td>{p.task_count}</td>
                        <td className="cost">${p.cost_usd.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {cost.daily_trend.length > 0 && (
            <div className="card mb-2">
              <h4>Daily Trend</h4>
              <table>
                <thead><tr><th>Date</th><th>API Calls</th><th>Cost</th></tr></thead>
                <tbody>
                  {cost.daily_trend.map(d => (
                    <tr key={d.date}>
                      <td>{d.date}</td>
                      <td>{d.api_calls}</td>
                      <td className="cost">${d.cost_usd.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="card mb-2">
            <span className="text-dim">Total Task Cost:</span>{' '}
            <span className="cost" style={{ fontSize: '1.25rem' }}>${cost.total_cost_usd.toFixed(4)}</span>
          </div>
        </>
      )}

      {/* Task Outcomes */}
      {outcomes && (
        <>
          <h3 className="mb-1">Task Outcomes</h3>
          <div className="grid grid-2 mb-2">
            <div className="card">
              <h4>Success Rate by Tier</h4>
              {outcomes.by_tier.length === 0 ? (
                <span className="text-dim">No data yet</span>
              ) : (
                <table>
                  <thead><tr><th>Tier</th><th>Total</th><th>OK</th><th>Failed</th><th>Review</th><th>Rate</th></tr></thead>
                  <tbody>
                    {outcomes.by_tier.map(t => (
                      <tr key={t.model_tier}>
                        <td><span className={`badge ${t.model_tier}`}>{t.model_tier}</span></td>
                        <td>{t.total}</td>
                        <td>{t.completed}</td>
                        <td>{t.failed}</td>
                        <td>{t.needs_review}</td>
                        <td>{(t.success_rate * 100).toFixed(0)}% {pctBar(t.success_rate, pctClass(t.success_rate))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="card">
              <h4>Verification Signal</h4>
              {outcomes.verification_by_tier.length === 0 ? (
                <span className="text-dim">No verified tasks yet</span>
              ) : (
                <table>
                  <thead><tr><th>Tier</th><th>Verified</th><th>Pass</th><th>Gaps</th><th>Human</th><th>Rate</th></tr></thead>
                  <tbody>
                    {outcomes.verification_by_tier.map(v => (
                      <tr key={v.model_tier}>
                        <td><span className={`badge ${v.model_tier}`}>{v.model_tier}</span></td>
                        <td>{v.total_verified}</td>
                        <td>{v.passed}</td>
                        <td>{v.gaps_found}</td>
                        <td>{v.human_needed}</td>
                        <td>{(v.pass_rate * 100).toFixed(0)}% {pctBar(v.pass_rate, pctClass(v.pass_rate))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}

      {/* Efficiency */}
      {efficiency && (
        <>
          <h3 className="mb-1">Efficiency</h3>
          <div className="grid grid-2 mb-2">
            <div className="card">
              <h4>Retries by Tier</h4>
              {efficiency.retries_by_tier.length === 0 ? (
                <span className="text-dim">No data yet</span>
              ) : (
                <table>
                  <thead><tr><th>Tier</th><th>Tasks</th><th>W/ Retries</th><th>Total Retries</th><th>Rate</th></tr></thead>
                  <tbody>
                    {efficiency.retries_by_tier.map(r => (
                      <tr key={r.model_tier}>
                        <td><span className={`badge ${r.model_tier}`}>{r.model_tier}</span></td>
                        <td>{r.total_tasks}</td>
                        <td>{r.tasks_with_retries}</td>
                        <td>{r.total_retries}</td>
                        <td>{(r.retry_rate * 100).toFixed(0)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="mt-1 text-dim">
                Checkpoints: {efficiency.checkpoint_count} total, {efficiency.unresolved_checkpoint_count} unresolved
              </div>
            </div>
            <div className="card">
              <h4>Wave Throughput</h4>
              {efficiency.wave_throughput.length === 0 ? (
                <span className="text-dim">No completed tasks yet</span>
              ) : (
                <table>
                  <thead><tr><th>Project</th><th>Wave</th><th>Tasks</th><th>Avg Duration</th></tr></thead>
                  <tbody>
                    {efficiency.wave_throughput.map(w => (
                      <tr key={`${w.project_id}-${w.wave}`}>
                        <td>{w.project_name}</td>
                        <td>Wave {w.wave}</td>
                        <td>{w.task_count}</td>
                        <td>{w.avg_duration_seconds != null ? `${w.avg_duration_seconds.toFixed(1)}s` : '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {efficiency.cost_efficiency.length > 0 && (
            <div className="card mb-2">
              <h4>Cost Efficiency</h4>
              <table>
                <thead><tr><th>Tier</th><th>Total Cost</th><th>Completed</th><th>Verified Pass</th><th>Cost/Pass</th></tr></thead>
                <tbody>
                  {efficiency.cost_efficiency.map(c => (
                    <tr key={c.model_tier}>
                      <td><span className={`badge ${c.model_tier}`}>{c.model_tier}</span></td>
                      <td className="cost">${c.cost_usd.toFixed(4)}</td>
                      <td>{c.tasks_completed}</td>
                      <td>{c.verification_pass_count}</td>
                      <td className="cost">{c.cost_per_pass != null ? `$${c.cost_per_pass.toFixed(4)}` : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  )
}
