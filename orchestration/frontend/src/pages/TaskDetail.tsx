// Orchestration Engine - Task Detail Page
//
// Depends on: api/client.ts, api/projects.ts, hooks/useFetch.ts,
//             components/CopyButton.tsx, components/Modal.tsx
// Used by:    App.tsx

import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { apiFetch, apiPatch, apiPost } from '../api/client'
import { reviewTask } from '../api/projects'
import { useFetch } from '../hooks/useFetch'
import CopyButton from '../components/CopyButton'
import Modal from '../components/Modal'
import type { Task } from '../types'

export default function TaskDetail() {
  const { id, taskId } = useParams<{ id: string; taskId: string }>()
  const { data: task, error, refetch } = useFetch<Task>(
    () => apiFetch<Task>(`/tasks/${taskId}`),
    [taskId],
  )
  const [feedback, setFeedback] = useState('')
  const [actionLoading, setActionLoading] = useState('')
  const [actionError, setActionError] = useState('')

  // Edit & Retry modal state
  const [editOpen, setEditOpen] = useState(false)
  const [editTitle, setEditTitle] = useState('')
  const [editDesc, setEditDesc] = useState('')

  if (!id || !taskId) return <div className="text-dim">Invalid URL — missing project or task ID.</div>
  if (error) return <div className="card" style={{ borderColor: 'var(--error)' }}>Error loading task: {error}</div>
  if (!task) return <div className="loading-spinner">Loading task...</div>

  const handleReview = async (action: 'approve' | 'retry') => {
    setActionLoading(action)
    setActionError('')
    try {
      await reviewTask(taskId!, action, action === 'retry' ? feedback : '')
      setFeedback('')
      refetch()
    } catch (e) {
      setActionError(String(e))
    }
    setActionLoading('')
  }

  const handleEditRetry = async () => {
    setActionLoading('edit')
    setActionError('')
    try {
      const updates: Record<string, string> = {}
      if (editTitle !== task.title) updates.title = editTitle
      if (editDesc !== task.description) updates.description = editDesc
      if (Object.keys(updates).length > 0) {
        await apiPatch(`/tasks/${taskId}`, updates)
      }
      if (task.status === 'failed') {
        await apiPost(`/tasks/${taskId}/retry`)
      }
      setEditOpen(false)
      refetch()
    } catch (e) {
      setActionError(String(e))
    }
    setActionLoading('')
  }

  const canEditRetry = ['failed', 'pending', 'blocked'].includes(task.status)

  return (
    <>
      <Link to={`/project/${id}`} className="text-dim text-sm">&larr; Back to project</Link>
      <div className="flex-between mb-2">
        <h2>{task.title}</h2>
        <div className="flex gap-1">
          {canEditRetry && (
            <button className="btn btn-secondary btn-sm" onClick={() => {
              setEditTitle(task.title)
              setEditDesc(task.description)
              setActionError('')
              setEditOpen(true)
            }}>Edit &amp; Retry</button>
          )}
          <span className={`badge ${task.status}`}>{task.status}</span>
        </div>
      </div>

      <div className="grid grid-4 mb-2">
        <div className="card">
          <h3>Model</h3>
          <span className={`badge ${task.model_tier}`}>{task.model_tier}</span>
          {task.model_used && <span className="text-dim text-sm" style={{ marginLeft: '0.5rem' }}>{task.model_used}</span>}
        </div>
        <div className="card">
          <h3>Cost</h3>
          <span className="cost">${task.cost_usd.toFixed(4)}</span>
        </div>
        <div className="card">
          <h3>Tokens</h3>
          <span className="text-sm">{task.prompt_tokens} in / {task.completion_tokens} out</span>
        </div>
        <div className="card">
          <h3>Type</h3>
          <span>{task.task_type}</span>
        </div>
      </div>

      {/* Review action panel */}
      {task.status === 'needs_review' && (
        <div className="card mb-2" style={{ borderColor: 'var(--warning)' }}>
          <h3>Review Required</h3>
          <p className="text-sm text-dim mb-1">
            This task needs your review. Approve the output or retry with feedback.
          </p>
          <div className="form-group">
            <label>Feedback (for retry)</label>
            <textarea value={feedback} onChange={e => setFeedback(e.target.value)}
              placeholder="Describe what needs to change..." style={{ minHeight: '60px' }} />
          </div>
          {actionError && <div className="text-sm mb-1" style={{ color: 'var(--error)' }}>{actionError}</div>}
          <div className="flex gap-1">
            <button className="btn btn-primary" onClick={() => handleReview('approve')}
              disabled={!!actionLoading}>
              {actionLoading === 'approve' ? 'Approving...' : 'Approve'}
            </button>
            <button className="btn btn-secondary" onClick={() => handleReview('retry')}
              disabled={!!actionLoading}>
              {actionLoading === 'retry' ? 'Retrying...' : 'Retry with Feedback'}
            </button>
          </div>
        </div>
      )}

      <div className="card mb-2">
        <h3>Description</h3>
        <p style={{ whiteSpace: 'pre-wrap' }}>{task.description}</p>
      </div>

      {task.tools.length > 0 && (
        <div className="card mb-2">
          <h3>Tools</h3>
          <div className="flex gap-1">
            {task.tools.map(t => <span key={t} className="badge">{t}</span>)}
          </div>
        </div>
      )}

      {/* Verification status */}
      {task.verification_status && (
        <div className={`verification-card ${task.verification_status}`}>
          <div className="flex-between mb-1">
            <h3 style={{ margin: 0 }}>Verification</h3>
            <span className={`badge ${task.verification_status}`}>
              {task.verification_status.replace('_', ' ')}
            </span>
          </div>
          {task.verification_notes && (
            <p className="text-sm" style={{ whiteSpace: 'pre-wrap' }}>{task.verification_notes}</p>
          )}
        </div>
      )}

      {task.error && (
        <div className="card mb-2" style={{ borderColor: 'var(--error)' }}>
          <div className="flex-between">
            <h3>Error</h3>
            <CopyButton text={task.error} label="Copy Error" />
          </div>
          <pre>{task.error}</pre>
        </div>
      )}

      {task.output_text && (
        <div className="card">
          <div className="flex-between">
            <h3>Output</h3>
            <CopyButton text={task.output_text} label="Copy Output" />
          </div>
          <pre style={{ maxHeight: '60vh', overflowY: 'auto' }}>{task.output_text}</pre>
        </div>
      )}

      {/* Edit & Retry modal */}
      <Modal open={editOpen} onClose={() => setEditOpen(false)} title="Edit Task">
        <div className="form-group">
          <label>Title</label>
          <input value={editTitle} onChange={e => setEditTitle(e.target.value)} />
        </div>
        <div className="form-group">
          <label>Description</label>
          <textarea value={editDesc} onChange={e => setEditDesc(e.target.value)}
            style={{ minHeight: '120px' }} />
        </div>
        {actionError && <div className="text-sm mb-1" style={{ color: 'var(--error)' }}>{actionError}</div>}
        <div className="flex gap-1">
          <button className="btn btn-primary" disabled={!!actionLoading} onClick={handleEditRetry}>
            {actionLoading === 'edit' ? 'Saving...' : (task.status === 'failed' ? 'Save & Retry' : 'Save')}
          </button>
          <button className="btn btn-secondary" onClick={() => setEditOpen(false)}>Cancel</button>
        </div>
      </Modal>
    </>
  )
}
