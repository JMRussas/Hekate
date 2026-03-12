// Orchestration Engine - RAG Database Browser
//
// Read-only UI for inspecting configured RAG databases.
// Indexing is managed externally (noz-rag / verse-rag pipelines).
//
// Depends on: api/rag.ts, hooks/useFetch.ts
// Used by:    App.tsx

import { useEffect, useState } from 'react'
import { useFetch } from '../hooks/useFetch'
import { listDatabases, listDocuments, RAGDatabaseInfo, RAGDocumentsResponse } from '../api/rag'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export default function RAG() {
  const { data: databases, loading, error } = useFetch<RAGDatabaseInfo[]>(listDatabases)
  const [selectedDb, setSelectedDb] = useState<string | null>(null)
  const [selectedSource, setSelectedSource] = useState<string | null>(null)
  const [docsPage, setDocsPage] = useState(0)
  const [docs, setDocs] = useState<RAGDocumentsResponse | null>(null)

  useEffect(() => {
    if (!selectedDb) {
      setDocs(null)
      return
    }
    let cancelled = false
    listDocuments(selectedDb, {
      source: selectedSource || undefined,
      offset: docsPage * 50,
      limit: 50,
    }).then(result => {
      if (!cancelled) setDocs(result)
    }).catch(() => {
      if (!cancelled) setDocs(null)
    })
    return () => { cancelled = true }
  }, [selectedDb, selectedSource, docsPage])

  const activeDb = databases?.find(d => d.name === selectedDb)

  return (
    <div>
      <h2>RAG Databases</h2>
      <p className="text-dim text-sm mb-2">
        Read-only inspection. Indexing is managed externally.
      </p>

      {loading && <p className="text-dim">Loading databases...</p>}
      {error && <p className="text-dim">Error: {error}</p>}

      {/* Database cards */}
      {databases && (
        <div className="grid grid-3 mb-2">
          {databases.map(db => (
            <div
              key={db.name}
              className="card"
              style={{ cursor: 'pointer', border: selectedDb === db.name ? '1px solid var(--accent)' : undefined }}
              onClick={() => {
                setSelectedDb(db.name === selectedDb ? null : db.name)
                setSelectedSource(null)
                setDocsPage(0)
              }}
            >
              <div className="flex-between mb-1">
                <h3 style={{ margin: 0 }}>{db.name}</h3>
                <span className={`badge ${db.exists ? db.index_status === 'loaded' ? 'online' : 'pending' : 'offline'}`}>
                  {db.exists ? db.index_status : 'missing'}
                </span>
              </div>
              <div className="text-sm text-dim">
                <div>{db.chunk_count.toLocaleString()} chunks</div>
                <div>{db.source_count} sources</div>
                <div>{formatBytes(db.file_size_bytes)}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Source browser */}
      {activeDb && activeDb.sources.length > 0 && (
        <div className="card">
          <h3>Sources in {activeDb.name}</h3>
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Chunks</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {activeDb.sources.map(s => (
                <tr key={s.source}>
                  <td>{s.source}</td>
                  <td>{s.count.toLocaleString()}</td>
                  <td>
                    <button
                      className="btn btn-sm btn-secondary"
                      onClick={() => {
                        setSelectedSource(selectedSource === s.source ? null : s.source)
                        setDocsPage(0)
                      }}
                    >
                      {selectedSource === s.source ? 'Clear' : 'Browse'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Document browser */}
      {selectedDb && docs && (
        <div className="card">
          <div className="flex-between mb-1">
            <h3>
              Chunks{selectedSource ? ` (${selectedSource})` : ''} — {docs.total.toLocaleString()} total
            </h3>
            <div className="flex gap-1">
              <button
                className="btn btn-sm btn-secondary"
                disabled={docsPage === 0}
                onClick={() => setDocsPage(p => p - 1)}
              >
                Prev
              </button>
              <span className="text-sm text-dim" style={{ lineHeight: '28px' }}>
                Page {docsPage + 1} of {Math.max(1, Math.ceil(docs.total / 50))}
              </span>
              <button
                className="btn btn-sm btn-secondary"
                disabled={(docsPage + 1) * 50 >= docs.total}
                onClick={() => setDocsPage(p => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Source</th>
                <th>Preview</th>
              </tr>
            </thead>
            <tbody>
              {docs.items.map(chunk => (
                <tr key={chunk.id}>
                  <td className="text-mono text-sm">{chunk.type_name || '—'}</td>
                  <td className="text-sm">{chunk.source}</td>
                  <td className="text-sm" style={{ maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {chunk.text_preview}
                  </td>
                </tr>
              ))}
              {docs.items.length === 0 && (
                <tr><td colSpan={3} className="text-dim">No chunks found.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
