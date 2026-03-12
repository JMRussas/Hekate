// Orchestration Engine - Services Page

import { useState } from 'react'
import { listServices } from '../api/services'
import { useFetch } from '../hooks/useFetch'
import type { Resource } from '../types'

export default function Services() {
  const { data: services, loading, error, refetch } = useFetch<Resource[]>(
    () => listServices(),
    [],
  )
  const [refreshing, setRefreshing] = useState(false)

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      await listServices(true)
      refetch()
    } catch { /* error handled by useFetch on next refetch */ }
    setRefreshing(false)
  }

  return (
    <>
      <div className="flex-between mb-2">
        <h2>Services</h2>
        <button className="btn btn-secondary btn-sm" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? 'Checking...' : 'Refresh'}
        </button>
      </div>

      {error && <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>Failed to load services: {error}</div>}

      {loading && !services ? (
        <div className="loading-spinner">Loading services...</div>
      ) : services && services.length === 0 ? (
        <div className="card text-dim">No services configured.</div>
      ) : (
        <div className="grid grid-3">
          {(services ?? []).map(s => (
            <div key={s.id} className="card">
              <div className="flex-between mb-1">
                <strong>{s.name}</strong>
                <span className={`badge ${s.status}`}>{s.status}</span>
              </div>
              <div className="text-sm text-dim">
                <span>Category: {s.category}</span>
                {s.method && <span> | Check: {s.method}</span>}
              </div>
              {s.details && Object.keys(s.details).length > 0 && (
                <pre className="text-sm" style={{ marginTop: '0.5rem' }}>
                  {JSON.stringify(s.details, null, 2)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  )
}
