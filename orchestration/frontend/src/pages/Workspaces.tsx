// Orchestration Engine - Workspaces Page
//
// Shows all working directories with their plans grouped underneath.
//
// Depends on: api/projects.ts, types/index.ts
// Used by:    App.tsx

import { Link } from 'react-router-dom'
import { listProjects } from '../api/projects'
import { useFetch } from '../hooks/useFetch'
import type { Project } from '../types'

interface Workspace {
  path: string
  displayPath: string
  plans: Project[]
  activePlans: number
  completedPlans: number
}

function groupByWorkspace(projects: Project[]): Workspace[] {
  const map = new Map<string, Project[]>()

  // Group by repo_path, unset paths go under "(no workspace)"
  for (const p of projects) {
    const key = p.repo_path || '(no workspace)'
    const list = map.get(key) || []
    list.push(p)
    map.set(key, list)
  }

  const workspaces: Workspace[] = []
  for (const [path, plans] of map) {
    // Show last 2 path segments as display name
    const parts = path.split(/[/\\]/)
    const displayPath = path === '(no workspace)' ? path : parts.slice(-2).join('/')

    workspaces.push({
      path,
      displayPath,
      plans: plans.sort((a, b) => b.created_at - a.created_at),
      activePlans: plans.filter(p => ['executing', 'ready', 'paused'].includes(p.status)).length,
      completedPlans: plans.filter(p => p.status === 'completed').length,
    })
  }

  // Sort: workspaces with active plans first, then by name
  return workspaces.sort((a, b) => {
    if (a.activePlans !== b.activePlans) return b.activePlans - a.activePlans
    return a.displayPath.localeCompare(b.displayPath)
  })
}

export default function Workspaces() {
  const { data: projects, loading, error } = useFetch<Project[]>(listProjects, [])

  const workspaces = groupByWorkspace(projects ?? [])

  return (
    <>
      <h2 className="mb-2">Workspaces</h2>

      {error && <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>Failed to load: {error}</div>}

      {loading && !projects ? (
        <div className="loading-spinner">Loading workspaces...</div>
      ) : workspaces.length === 0 ? (
        <div className="card text-dim">No workspaces yet. Create a plan with a working directory to get started.</div>
      ) : (
        workspaces.map(ws => (
          <div key={ws.path} className="card mb-2">
            <div className="flex-between mb-1">
              <div>
                <h3 style={{ fontFamily: 'monospace', fontSize: '1rem' }}>{ws.displayPath}</h3>
                {ws.path !== '(no workspace)' && (
                  <span className="text-dim text-sm" style={{ fontFamily: 'monospace' }}>{ws.path}</span>
                )}
              </div>
              <div className="flex gap-1">
                {ws.activePlans > 0 && (
                  <span className="badge executing">{ws.activePlans} active</span>
                )}
                {ws.completedPlans > 0 && (
                  <span className="badge completed">{ws.completedPlans} done</span>
                )}
              </div>
            </div>

            <table>
              <thead>
                <tr>
                  <th>Plan</th><th>Status</th><th>Tasks</th><th>Rigor</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {ws.plans.map(p => (
                  <tr key={p.id}>
                    <td><Link to={`/project/${p.id}`}>{p.name}</Link></td>
                    <td><span className={`badge ${p.status}`}>{p.status}</span></td>
                    <td>
                      {p.task_summary ? (
                        <span className="text-sm">
                          {p.task_summary.completed}/{p.task_summary.total}
                          {p.task_summary.running > 0 && <span className="text-dim"> ({p.task_summary.running} running)</span>}
                        </span>
                      ) : '—'}
                    </td>
                    <td><span className={`badge rigor-${p.planning_rigor?.toLowerCase() ?? 'l2'}`}>{p.planning_rigor ?? 'L2'}</span></td>
                    <td className="text-dim text-sm">
                      {new Date(p.created_at * 1000).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
    </>
  )
}
