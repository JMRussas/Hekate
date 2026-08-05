// Pipeline Observatory — Full execution observability page
//
// Shows every relay event for a project: handler timings, gate decisions,
// CLI prompts, dispatch reasoning, verification verdicts, state transitions.

import { useState, useMemo, useCallback } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useFetch } from '../hooks/useFetch'
import { useObservatoryEvents } from '../hooks/useObservatoryEvents'
import { apiFetch } from '../api/client'
import type { Project, Task, EventFilters, EventCategory } from '../types'
import { EventTimeline } from '../components/Observatory/EventTimeline'
import { EventFilterBar } from '../components/Observatory/EventFilterBar'
import { getEventCategory } from '../components/Observatory/categories'

interface ProjectData {
  project: Project
  tasks: Task[]
}

async function fetchProjectData(id: string): Promise<ProjectData> {
  const [project, tasks] = await Promise.all([
    apiFetch<Project>(`/projects/${id}`),
    apiFetch<Task[]>(`/tasks/project/${id}`),
  ])
  return { project, tasks }
}

const DEFAULT_FILTERS: EventFilters = {
  categories: new Set<EventCategory>(),
  sources: new Set<string>(),
  severities: new Set<string>(),
  search: '',
  followTail: true,
}

export default function Observatory() {
  const { id } = useParams<{ id: string }>()
  const { data, loading, error } = useFetch(() => fetchProjectData(id!), [id])
  const { events, connected } = useObservatoryEvents(id || null)
  const [filters, setFilters] = useState<EventFilters>(DEFAULT_FILTERS)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)

  // Compute category counts for filter chips
  const categoryCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ev of events) {
      const cat = getEventCategory(ev.event_type)
      counts[cat] = (counts[cat] || 0) + 1
    }
    return counts
  }, [events])

  // Task click handler
  const onTaskClick = useCallback((taskId: string) => {
    setSelectedTaskId(prev => prev === taskId ? null : taskId)
  }, [])

  if (loading) return <div className="loading">Loading observatory...</div>
  if (error) return <div className="error-message">{error}</div>
  if (!data) return null

  const { project, tasks } = data

  return (
    <div className="observatory-page">
      {/* Header */}
      <div className="observatory-header">
        <div className="flex-between">
          <div>
            <Link to={`/project/${id}`} className="text-dim text-sm">&larr; Back to project</Link>
            <h2>{project.name}</h2>
          </div>
          <div className="flex gap-1">
            <span className={`badge ${project.status}`}>{project.status}</span>
            <span className={`badge ${connected ? 'completed' : 'failed'}`}>
              {connected ? 'Connected' : 'Disconnected'}
            </span>
            <span className="text-dim text-sm">{events.length} events</span>
          </div>
        </div>
      </div>

      {/* Filter bar */}
      <EventFilterBar
        filters={filters}
        onChange={setFilters}
        eventTypeCounts={categoryCounts}
      />

      {/* Main content: timeline + sidebar */}
      <div className="observatory-layout">
        {/* Timeline (left) */}
        <div className="observatory-main">
          <EventTimeline
            events={events}
            filters={filters}
            highlightTaskId={selectedTaskId}
          />
        </div>

        {/* Sidebar (right) */}
        <div className="observatory-sidebar">
          {/* Pipeline stages */}
          <div className="card">
            <h3 className="text-sm mb-1">Pipeline Stages</h3>
            <div className="pipeline-stages">
              {['Athena', 'Odin', 'Hermes', 'Mimir', 'Lifecycle'].map((stage, i) => {
                // Check if this stage has recent activity
                const stageSource = stage.toLowerCase()
                const isActive = events.length > 0 &&
                  events.slice(-20).some(e => e.source.includes(stageSource))
                return (
                  <div key={stage}>
                    {i > 0 && <span className="pipeline-stage-arrow">&rarr;</span>}
                    <span className={`pipeline-stage ${isActive ? 'active' : ''}`}>
                      {stage}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>

          {/* Tasks by wave */}
          <div className="card">
            <h3 className="text-sm mb-1">Tasks</h3>
            <div className="task-state-panel">
              {tasks.map(task => (
                <div
                  key={task.id}
                  className={`task-state-row ${selectedTaskId === task.id ? 'selected' : ''}`}
                  onClick={() => onTaskClick(task.id)}
                  title={task.title}
                >
                  <span className={`badge ${task.status}`}>{task.status}</span>
                  <span className="task-state-title">{task.title}</span>
                  <span className="text-dim text-sm">w{task.wave}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Last prompt (if any cli_prompt events) */}
          {(() => {
            const promptEvents = events.filter(e =>
              e.event_type === 'cli_prompt' &&
              (!selectedTaskId || (e.payload?.task_id as string)?.startsWith(selectedTaskId))
            )
            const lastPrompt = promptEvents[promptEvents.length - 1]
            if (!lastPrompt) return null
            return (
              <div className="card">
                <h3 className="text-sm mb-1">Last CLI Prompt</h3>
                <div className="prompt-panel">
                  <div className="prompt-meta text-dim text-sm">
                    Task: {(lastPrompt.payload.task_id as string)?.slice(0, 8)} |
                    Provider: {lastPrompt.payload.provider as string} |
                    {String(lastPrompt.payload.prompt_length)} chars
                  </div>
                  <pre className="prompt-text">
                    {lastPrompt.payload.prompt_text as string}
                  </pre>
                </div>
              </div>
            )
          })()}
        </div>
      </div>
    </div>
  )
}
