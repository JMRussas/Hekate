// Orchestration Engine - Dashboard Page

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listProjects, createProject } from '../api/projects'
import { getBudget } from '../api/usage'
import { listServices } from '../api/services'
import { useFetch } from '../hooks/useFetch'
import type { Project, BudgetStatus, Resource, PlanningRigor } from '../types'

interface DashboardData {
  projects: Project[]
  budget: BudgetStatus | null
  services: Resource[]
}

export default function Dashboard() {
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [requirements, setRequirements] = useState('')
  const [rigor, setRigor] = useState<PlanningRigor>('L2')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()

  const { data, loading: fetchLoading, error: fetchError } = useFetch<DashboardData>(
    () => Promise.all([
      listProjects(),
      getBudget().catch(() => null),
      listServices().catch(() => []),
    ]).then(([projects, budget, services]) => ({ projects, budget, services })),
    [],
  )

  const projects = data?.projects ?? []
  const budget = data?.budget ?? null
  const services = data?.services ?? []

  const handleCreate = async () => {
    if (!name.trim() || !requirements.trim()) {
      setError('Name and requirements are required.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const project = await createProject(name, requirements, rigor)
      navigate(`/project/${project.id}`)
    } catch (e) {
      setError(String(e))
    }
    setLoading(false)
  }

  const pctClass = (pct: number) => pct >= 100 ? 'danger' : pct >= 80 ? 'warn' : 'ok'

  return (
    <>
      <div className="flex-between mb-2">
        <h2>Projects</h2>
        <button className="btn btn-primary" onClick={() => setShowForm(!showForm)}>
          + New Project
        </button>
      </div>

      {fetchError && <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>Failed to load data: {fetchError}</div>}

      {showForm && (
        <div className="card mb-2">
          <div className="form-group">
            <label>Project Name</label>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="My Project" />
          </div>
          <div className="form-group">
            <label>Requirements</label>
            <textarea value={requirements} onChange={e => setRequirements(e.target.value)}
              placeholder="Describe what you want built..." />
          </div>
          <div className="form-group">
            <label>Planning Rigor</label>
            <select value={rigor} onChange={e => setRigor(e.target.value as PlanningRigor)}>
              <option value="L1">L1 Quick — Flat task list</option>
              <option value="L2">L2 Standard — Phases + open questions</option>
              <option value="L3">L3 Thorough — Phases + risk + test strategy</option>
            </select>
          </div>
          {error && <div className="text-sm" style={{ color: 'var(--error)', marginBottom: '0.5rem' }}>{error}</div>}
          <div className="flex gap-1">
            <button className="btn btn-primary" onClick={handleCreate} disabled={loading}>
              {loading ? 'Creating...' : 'Create Project'}
            </button>
            <button className="btn btn-secondary" onClick={() => setShowForm(false)}>Cancel</button>
          </div>
        </div>
      )}

      <div className="grid grid-3 mb-2">
        {budget && (
          <>
            <div className="card">
              <h3>Daily Budget</h3>
              <div className="flex-between mb-1">
                <span className="cost">${budget.daily_spent_usd.toFixed(2)}</span>
                <span className="text-dim text-sm">/ ${budget.daily_limit_usd.toFixed(2)}</span>
              </div>
              <div className="progress-bar">
                <div className={`progress-fill ${pctClass(budget.daily_pct)}`}
                  style={{ width: `${Math.min(budget.daily_pct, 100)}%` }} />
              </div>
            </div>
            <div className="card">
              <h3>Monthly Budget</h3>
              <div className="flex-between mb-1">
                <span className="cost">${budget.monthly_spent_usd.toFixed(2)}</span>
                <span className="text-dim text-sm">/ ${budget.monthly_limit_usd.toFixed(2)}</span>
              </div>
              <div className="progress-bar">
                <div className={`progress-fill ${pctClass(budget.monthly_pct)}`}
                  style={{ width: `${Math.min(budget.monthly_pct, 100)}%` }} />
              </div>
            </div>
          </>
        )}
        <div className="card">
          <h3>Services</h3>
          {fetchLoading && !data ? (
            <span className="text-dim text-sm">Checking...</span>
          ) : services.length === 0 ? (
            <span className="text-dim text-sm">No services configured</span>
          ) : (
            <div className="flex gap-1" style={{ flexWrap: 'wrap' }}>
              {services.map(s => (
                <span key={s.id} className={`badge ${s.status}`}>{s.name.split(' (')[0]}</span>
              ))}
            </div>
          )}
        </div>
      </div>

      {fetchLoading && !data ? (
        <div className="loading-spinner">Loading projects...</div>
      ) : projects.length === 0 ? (
        <div className="card text-dim">No projects yet. Create one to get started.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Rigor</th><th>Status</th><th>Tasks</th><th>Created</th>
            </tr>
          </thead>
          <tbody>
            {projects.map(p => (
              <tr key={p.id}>
                <td><Link to={`/project/${p.id}`}>{p.name}</Link></td>
                <td><span className={`badge rigor-${p.planning_rigor?.toLowerCase() ?? 'l2'}`}>{p.planning_rigor ?? 'L2'}</span></td>
                <td><span className={`badge ${p.status}`}>{p.status}</span></td>
                <td>
                  {p.task_summary ? (
                    <span className="text-sm">
                      {p.task_summary.completed}/{p.task_summary.total}
                      {p.task_summary.running > 0 && <span className="text-dim"> ({p.task_summary.running} running)</span>}
                    </span>
                  ) : '—'}
                </td>
                <td className="text-dim text-sm">
                  {new Date(p.created_at * 1000).toLocaleDateString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
