// Orchestration Engine - Admin Dashboard
//
// Admin-only page: user management, system stats, model spend breakdown.
//
// Depends on: api/admin.ts, hooks/useFetch.ts
// Used by:    App.tsx

import { useState, useCallback } from 'react'
import { useFetch } from '../hooks/useFetch'
import { listUsers, updateUser, getStats } from '../api/admin'
import type { AdminUser, AdminStats } from '../api/admin'

function formatDate(ts: number | null): string {
  if (!ts) return 'Never'
  return new Date(ts * 1000).toLocaleDateString()
}

export default function Admin() {
  const { data: users, loading: usersLoading, error: usersError, refetch: refetchUsers } = useFetch<AdminUser[]>(listUsers)
  const { data: stats, loading: statsLoading, error: statsError } = useFetch<AdminStats>(getStats)
  const [updating, setUpdating] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const handleToggleActive = useCallback(async (user: AdminUser) => {
    setUpdating(user.id)
    setActionError(null)
    try {
      await updateUser(user.id, { is_active: !user.is_active })
      refetchUsers()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Update failed')
    } finally {
      setUpdating(null)
    }
  }, [refetchUsers])

  const handleChangeRole = useCallback(async (user: AdminUser, role: 'admin' | 'user') => {
    setUpdating(user.id)
    setActionError(null)
    try {
      await updateUser(user.id, { role })
      refetchUsers()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Update failed')
    } finally {
      setUpdating(null)
    }
  }, [refetchUsers])

  return (
    <div>
      <h1 className="mb-2">Admin Dashboard</h1>

      {/* System Stats */}
      <div className="card mb-2">
        <h2>System Stats</h2>
        {statsLoading && <p className="text-dim">Loading stats...</p>}
        {statsError && <p className="text-dim">Failed to load stats: {statsError}</p>}
        {stats && (
          <>
            <div className="grid grid-4 mb-2">
              <div className="card">
                <h3>Users</h3>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>{stats.total_users}</div>
                <div className="text-dim text-sm">{stats.active_users} active</div>
              </div>
              <div className="card">
                <h3>Projects</h3>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>{stats.total_projects}</div>
                <div className="text-dim text-sm">
                  {Object.entries(stats.projects_by_status).map(([s, c]) => `${c} ${s}`).join(', ') || 'none'}
                </div>
              </div>
              <div className="card">
                <h3>Tasks</h3>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>{stats.total_tasks}</div>
                <div className="text-dim text-sm">
                  {stats.task_completion_rate > 0
                    ? `${(stats.task_completion_rate * 100).toFixed(1)}% completion`
                    : 'no completions yet'}
                </div>
              </div>
              <div className="card">
                <h3>Total Spend</h3>
                <div className="cost" style={{ fontSize: '1.5rem' }}>${stats.total_spend_usd.toFixed(4)}</div>
              </div>
            </div>

            {/* Model Breakdown */}
            {Object.keys(stats.spend_by_model).length > 0 && (
              <div className="mb-2">
                <h3>Spend by Model</h3>
                <table>
                  <thead>
                    <tr><th>Model</th><th>Spend (USD)</th></tr>
                  </thead>
                  <tbody>
                    {Object.entries(stats.spend_by_model).map(([model, spend]) => (
                      <tr key={model}>
                        <td>{model}</td>
                        <td className="cost">${spend.toFixed(6)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      {/* Users */}
      <div className="card">
        <h2>Users</h2>
        {actionError && <div className="auth-error mb-1">{actionError}</div>}
        {usersLoading && <p className="text-dim">Loading users...</p>}
        {usersError && <p className="text-dim">Failed to load users: {usersError}</p>}
        {users && (
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Role</th>
                <th>Active</th>
                <th>Projects</th>
                <th>Last Login</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id}>
                  <td>{u.email}</td>
                  <td>{u.display_name || <span className="text-dim">-</span>}</td>
                  <td><span className={`badge ${u.role}`}>{u.role}</span></td>
                  <td>
                    <span className={`badge ${u.is_active ? 'online' : 'offline'}`}>
                      {u.is_active ? 'yes' : 'no'}
                    </span>
                  </td>
                  <td>{u.project_count}</td>
                  <td className="text-dim text-sm">{formatDate(u.last_login_at)}</td>
                  <td>
                    <div className="flex gap-1">
                      <button
                        className="btn btn-sm btn-secondary"
                        disabled={updating === u.id}
                        onClick={() => handleToggleActive(u)}
                      >
                        {u.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                      <select
                        value={u.role}
                        disabled={updating === u.id}
                        onChange={e => handleChangeRole(u, e.target.value as 'admin' | 'user')}
                        style={{ width: 'auto', padding: '0.2rem 0.4rem', fontSize: '0.75rem' }}
                      >
                        <option value="user">user</option>
                        <option value="admin">admin</option>
                      </select>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
