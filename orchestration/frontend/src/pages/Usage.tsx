// Orchestration Engine - Usage & Budget Page

import { useEffect, useState } from 'react'
import { getBudget, getUsageSummary, getDailyUsage, getUsageByProject } from '../api/usage'
import type { BudgetStatus, UsageSummary } from '../types'

export default function Usage() {
  const [budget, setBudget] = useState<BudgetStatus | null>(null)
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [daily, setDaily] = useState<{ date: string; cost_usd: number; api_calls: number }[]>([])
  const [byProject, setByProject] = useState<{ project_name: string; cost_usd: number }[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([
      getBudget().then(setBudget),
      getUsageSummary().then(setUsage),
      getDailyUsage(14).then(setDaily),
      getUsageByProject().then(setByProject),
    ]).catch(e => setError(String(e)))
  }, [])

  const pctClass = (pct: number) => pct >= 100 ? 'danger' : pct >= 80 ? 'warn' : 'ok'

  return (
    <>
      <h2 className="mb-2">Usage & Budget</h2>

      {error && <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>Failed to load usage data: {error}</div>}

      {budget && (
        <div className="grid grid-2 mb-2">
          <div className="card">
            <h3>Daily Spend</h3>
            <div className="flex-between mb-1">
              <span className="cost" style={{ fontSize: '1.5rem' }}>${budget.daily_spent_usd.toFixed(2)}</span>
              <span className="text-dim">/ ${budget.daily_limit_usd.toFixed(2)} ({budget.daily_pct.toFixed(0)}%)</span>
            </div>
            <div className="progress-bar" role="progressbar"
              aria-valuenow={Math.round(budget.daily_pct)} aria-valuemin={0} aria-valuemax={100}
              aria-label="Daily budget usage">
              <div className={`progress-fill ${pctClass(budget.daily_pct)}`}
                style={{ width: `${Math.min(budget.daily_pct, 100)}%` }} />
            </div>
          </div>
          <div className="card">
            <h3>Monthly Spend</h3>
            <div className="flex-between mb-1">
              <span className="cost" style={{ fontSize: '1.5rem' }}>${budget.monthly_spent_usd.toFixed(2)}</span>
              <span className="text-dim">/ ${budget.monthly_limit_usd.toFixed(2)} ({budget.monthly_pct.toFixed(0)}%)</span>
            </div>
            <div className="progress-bar" role="progressbar"
              aria-valuenow={Math.round(budget.monthly_pct)} aria-valuemin={0} aria-valuemax={100}
              aria-label="Monthly budget usage">
              <div className={`progress-fill ${pctClass(budget.monthly_pct)}`}
                style={{ width: `${Math.min(budget.monthly_pct, 100)}%` }} />
            </div>
          </div>
        </div>
      )}

      {usage && (
        <div className="grid grid-2 mb-2">
          <div className="card">
            <h3>By Model</h3>
            {Object.keys(usage.by_model).length === 0 ? (
              <span className="text-dim">No usage yet</span>
            ) : (
              <table>
                <thead><tr><th>Model</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead>
                <tbody>
                  {Object.entries(usage.by_model).map(([model, data]) => (
                    <tr key={model}>
                      <td className="text-sm">{model}</td>
                      <td>{data.calls}</td>
                      <td className="text-sm">{(data.prompt_tokens + data.completion_tokens).toLocaleString()}</td>
                      <td className="cost">${data.cost_usd.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="card">
            <h3>By Project</h3>
            {byProject.length === 0 ? (
              <span className="text-dim">No usage yet</span>
            ) : (
              <table>
                <thead><tr><th>Project</th><th>Cost</th></tr></thead>
                <tbody>
                  {byProject.map((p, i) => (
                    <tr key={i}>
                      <td>{p.project_name}</td>
                      <td className="cost">${p.cost_usd.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {daily.length > 0 && (
        <div className="card">
          <h3>Daily History (last 14 days)</h3>
          <table>
            <thead><tr><th>Date</th><th>API Calls</th><th>Cost</th></tr></thead>
            <tbody>
              {daily.map(d => (
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

      {usage && (
        <div className="card" style={{ marginTop: '1rem' }}>
          <h3>Totals</h3>
          <div className="grid grid-4">
            <div><span className="text-dim">Total Cost</span><br /><span className="cost">${usage.total_cost_usd.toFixed(4)}</span></div>
            <div><span className="text-dim">API Calls</span><br />{usage.api_call_count}</div>
            <div><span className="text-dim">Input Tokens</span><br />{usage.total_prompt_tokens.toLocaleString()}</div>
            <div><span className="text-dim">Output Tokens</span><br />{usage.total_completion_tokens.toLocaleString()}</div>
          </div>
        </div>
      )}
    </>
  )
}
