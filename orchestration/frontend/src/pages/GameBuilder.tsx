// Game Builder — NoZ game development director interface
//
// Pre-configures project creation for NoZ (hybrid execution, game repo path),
// provides a director chat for natural language game instructions, and shows
// live task progress via SSE.
//
// Depends on: api/projects.ts, hooks/useSSE.ts, hooks/useFetch.ts, types
// Used by:    App.tsx

import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listProjects, createProject, generatePlan, approvePlan, startExecution,
  listPlans, listTasks, pauseExecution, sendChat, updateProject } from '../api/projects'
import type { ChatMessage as ApiChatMessage } from '../api/projects'
import { useFetch } from '../hooks/useFetch'
import { useSSE } from '../hooks/useSSE'
import type { Project, Task, SSEEvent } from '../types'

const NOZ_REPO_PATH = '//SISYPHUS/Shared/noz-cs'
const CONTEXT_STORE_URL = '/context-api'

// --- Asset browser (context store) ---

interface KnowledgeNode {
  id: string
  name: string
  type: string
  value?: string
}

async function fetchKnowledgeTree(): Promise<KnowledgeNode[]> {
  try {
    // Find the NoZ Engine Overview root node
    const rootsResp = await fetch(`${CONTEXT_STORE_URL}/api/nodes/roots`)
    if (!rootsResp.ok) return []
    const roots: KnowledgeNode[] = await rootsResp.json()
    const nozRoot = roots.find(r => r.name === 'NoZ Engine Overview')
    if (!nozRoot) return []

    const childrenResp = await fetch(`${CONTEXT_STORE_URL}/api/node/${nozRoot.id}/children`)
    if (!childrenResp.ok) return []
    return await childrenResp.json()
  } catch {
    return []
  }
}

async function fetchNodeChildren(nodeId: string): Promise<KnowledgeNode[]> {
  try {
    const resp = await fetch(`${CONTEXT_STORE_URL}/api/node/${nodeId}/children`)
    if (!resp.ok) return []
    return await resp.json()
  } catch {
    return []
  }
}

// --- Director chat message types ---

interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: number
}

// --- Main component ---

export default function GameBuilder() {
  const navigate = useNavigate()

  // Active project state
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const [view, setView] = useState<'projects' | 'director'>('projects')

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: 'system', content: 'Game Builder ready. Create a new game project or select an existing one to start directing.', timestamp: Date.now() },
  ])
  const [input, setInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const chatEndRef = useRef<HTMLDivElement>(null)

  // Knowledge browser
  const [knowledgeNodes, setKnowledgeNodes] = useState<KnowledgeNode[]>([])
  const [expandedNodes, setExpandedNodes] = useState<Record<string, KnowledgeNode[]>>({})
  const [showKnowledge, setShowKnowledge] = useState(false)

  // SSE for active project
  const { events, connected } = useSSE(activeProjectId)

  // Fetch projects
  const { data: projects, loading: projectsLoading, refetch: refetchProjects } = useFetch(
    () => listProjects(),
    [],
  )

  // Fetch tasks for active project
  const { data: tasks, refetch: refetchTasks } = useFetch(
    () => activeProjectId ? listTasks(activeProjectId, { exclude_output: true }) : Promise.resolve([]),
    [activeProjectId],
  )

  // Load knowledge on mount
  useEffect(() => {
    fetchKnowledgeTree().then(setKnowledgeNodes)
  }, [])

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // SSE event handler — push events as system messages
  useEffect(() => {
    if (events.length === 0) return
    const latest = events[events.length - 1]
    const eventMsg = formatSSEEvent(latest)
    if (eventMsg) {
      setMessages(prev => [...prev, { role: 'system', content: eventMsg, timestamp: Date.now() }])
      refetchTasks()
    }
  }, [events.length])

  // --- Project creation (game-dev defaults) ---
  const [gameName, setGameName] = useState('')
  const [gameDesc, setGameDesc] = useState('')
  const [creating, setCreating] = useState(false)

  const handleCreateGame = async () => {
    if (!gameName.trim() || !gameDesc.trim()) return
    setCreating(true)
    try {
      const project = await createProject({
        name: gameName,
        requirements: `NoZ 2D game project.\n\n${gameDesc}`,
        planning_rigor: 'L2',
        repo_path: NOZ_REPO_PATH,
        config: { execution_mode: 'hybrid' },
      })
      setActiveProjectId(project.id)
      setView('director')
      setMessages(prev => [...prev, {
        role: 'system',
        content: `Game project "${gameName}" created. Planning will generate tasks. Use the chat to direct development.`,
        timestamp: Date.now(),
      }])
      refetchProjects()

      // Auto-plan
      setMessages(prev => [...prev, { role: 'system', content: 'Generating plan...', timestamp: Date.now() }])
      const planResult = await generatePlan(project.id)
      setMessages(prev => [...prev, {
        role: 'system',
        content: `Plan generated (cost: $${planResult.cost_usd?.toFixed(4) ?? '?'}). Review tasks below, then start execution.`,
        timestamp: Date.now(),
      }])
      refetchTasks()
    } catch (e) {
      setMessages(prev => [...prev, { role: 'system', content: `Error: ${e}`, timestamp: Date.now() }])
    }
    setCreating(false)
  }

  // --- Director actions ---

  const handleStartExecution = async () => {
    if (!activeProjectId) return
    try {
      // Approve latest plan first
      const plans = await listPlans(activeProjectId)
      const draft = plans.find(p => p.status === 'draft')
      if (draft) {
        await approvePlan(activeProjectId, draft.id)
      }
      await startExecution(activeProjectId)
      setMessages(prev => [...prev, { role: 'system', content: 'Execution started. Tasks are being dispatched.', timestamp: Date.now() }])
      refetchTasks()
    } catch (e) {
      setMessages(prev => [...prev, { role: 'system', content: `Error starting: ${e}`, timestamp: Date.now() }])
    }
  }

  const handlePause = async () => {
    if (!activeProjectId) return
    try {
      await pauseExecution(activeProjectId)
      setMessages(prev => [...prev, { role: 'system', content: 'Execution paused.', timestamp: Date.now() }])
    } catch (e) {
      setMessages(prev => [...prev, { role: 'system', content: `Error: ${e}`, timestamp: Date.now() }])
    }
  }

  // --- Chat send ---

  const handleSend = async () => {
    if (!input.trim()) return
    const userMsg = input.trim()
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: userMsg, timestamp: Date.now() }])

    if (!activeProjectId) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: 'No active project. Create a game project first, or select one from the list.',
        timestamp: Date.now(),
      }])
      return
    }

    setChatLoading(true)
    try {
      // Build game-dev context for the system prompt
      const taskList = (tasks ?? []).map(t => `[${t.status}] ${t.title}`).join('\n')
      const context = [
        'You are a game development director assistant for a NoZ 2D game engine project.',
        'NoZ is a C# immediate-mode 2D engine with WebGPU rendering, skeletal animation, VFX particles, and an immediate-mode UI system.',
        'Games implement IApplication with Update(), UpdateUI(), FixedUpdate() lifecycle methods.',
        'Asset types: Sprite, Skeleton, Animation, VFX, Font, Shader, Sound, Atlas, Bundle.',
        activeProject ? `Project: ${activeProject.name} (${activeProject.status})` : '',
        taskList ? `Current tasks:\n${taskList}` : '',
        'If the user request requires code changes or new features, respond with a clear description of what to build.',
        'If it requires replanning, start your response with [REPLAN] followed by updated requirements.',
      ].filter(Boolean).join('\n\n')

      // Convert chat history to API format (exclude system messages, keep last 20)
      const apiMessages: ApiChatMessage[] = messages
        .filter(m => m.role !== 'system')
        .slice(-20)
        .map(m => ({ role: m.role, content: m.content }))

      const response = await sendChat({
        prompt: userMsg,
        context,
        provider: 'claude',
        messages: apiMessages,
      })

      const aiText = response.response

      // Check for [REPLAN] tag — AI wants to regenerate the plan
      if (aiText.startsWith('[REPLAN]')) {
        const newDirection = aiText.slice('[REPLAN]'.length).trim()
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `Replanning: ${newDirection}`,
          timestamp: Date.now(),
        }])

        try {
          // Append direction to project requirements
          const currentReqs = activeProject?.requirements ?? ''
          await updateProject(activeProjectId, {
            requirements: `${currentReqs}\n\nDirector update: ${newDirection}`,
          })

          // Generate new plan
          setMessages(prev => [...prev, {
            role: 'system',
            content: 'Generating updated plan...',
            timestamp: Date.now(),
          }])
          const planResult = await generatePlan(activeProjectId)
          setMessages(prev => [...prev, {
            role: 'system',
            content: `New plan generated (cost: $${planResult.cost_usd?.toFixed(4) ?? '?'}). Review and start execution when ready.`,
            timestamp: Date.now(),
          }])
          refetchTasks()
        } catch (planErr) {
          setMessages(prev => [...prev, {
            role: 'system',
            content: `Replan error: ${planErr instanceof Error ? planErr.message : String(planErr)}`,
            timestamp: Date.now(),
          }])
        }
      } else {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: aiText,
          timestamp: Date.now(),
        }])
      }
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${e instanceof Error ? e.message : String(e)}`,
        timestamp: Date.now(),
      }])
    }
    setChatLoading(false)
  }

  // --- Knowledge browser toggle ---

  const toggleNodeExpand = async (nodeId: string) => {
    if (expandedNodes[nodeId]) {
      const next = { ...expandedNodes }
      delete next[nodeId]
      setExpandedNodes(next)
    } else {
      const children = await fetchNodeChildren(nodeId)
      setExpandedNodes(prev => ({ ...prev, [nodeId]: children }))
    }
  }

  // --- Task summary ---
  const taskSummary = (tasks ?? []).reduce((acc, t) => {
    acc[t.status] = (acc[t.status] || 0) + 1
    return acc
  }, {} as Record<string, number>)

  const activeProject = (projects ?? []).find(p => p.id === activeProjectId)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div className="flex-between mb-2">
        <div className="flex gap-1" style={{ alignItems: 'center' }}>
          <h2>Game Builder</h2>
          {activeProject && (
            <span className={`badge ${activeProject.status}`} style={{ marginLeft: 8 }}>
              {activeProject.name} — {activeProject.status}
            </span>
          )}
          {connected && <span className="badge available" style={{ marginLeft: 4 }}>live</span>}
        </div>
        <div className="flex gap-1">
          <button className={`btn ${view === 'projects' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setView('projects')}>Projects</button>
          <button className={`btn ${view === 'director' ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setView('director')} disabled={!activeProjectId}>Director</button>
          <button className={`btn btn-secondary`}
            onClick={() => setShowKnowledge(!showKnowledge)}>
            {showKnowledge ? 'Hide' : 'Show'} Knowledge
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', flex: 1, gap: '1rem', minHeight: 0 }}>
        {/* Main panel */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          {view === 'projects' ? (
            /* --- Projects View --- */
            <div style={{ flex: 1, overflow: 'auto' }}>
              {/* New game form */}
              <div className="card mb-2">
                <h3>New Game</h3>
                <div className="form-group">
                  <label>Game Name</label>
                  <input value={gameName} onChange={e => setGameName(e.target.value)}
                    placeholder="My Awesome Game" />
                </div>
                <div className="form-group">
                  <label>Description</label>
                  <textarea value={gameDesc} onChange={e => setGameDesc(e.target.value)}
                    placeholder="A 2D platformer with enemies, power-ups, and boss fights..."
                    rows={3} />
                </div>
                <button className="btn btn-primary" onClick={handleCreateGame}
                  disabled={creating || !gameName.trim() || !gameDesc.trim()}>
                  {creating ? 'Creating...' : 'Create Game Project'}
                </button>
              </div>

              {/* Existing game projects */}
              {projectsLoading ? (
                <div className="loading-spinner">Loading...</div>
              ) : (projects ?? []).length === 0 ? (
                <div className="card text-dim">No projects yet.</div>
              ) : (
                <table>
                  <thead>
                    <tr><th>Game</th><th>Status</th><th>Tasks</th><th>Actions</th></tr>
                  </thead>
                  <tbody>
                    {(projects ?? []).map(p => (
                      <tr key={p.id}>
                        <td>{p.name}</td>
                        <td><span className={`badge ${p.status}`}>{p.status}</span></td>
                        <td className="text-sm">
                          {p.task_summary
                            ? `${p.task_summary.completed}/${p.task_summary.total}`
                            : '—'}
                        </td>
                        <td>
                          <div className="flex gap-1">
                            <button className="btn btn-primary" style={{ padding: '2px 8px', fontSize: '0.8rem' }}
                              onClick={() => { setActiveProjectId(p.id); setView('director') }}>
                              Direct
                            </button>
                            <Link to={`/project/${p.id}`} className="btn btn-secondary"
                              style={{ padding: '2px 8px', fontSize: '0.8rem', textDecoration: 'none' }}>
                              Detail
                            </Link>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ) : (
            /* --- Director View --- */
            <>
              {/* Task summary bar */}
              <div className="card mb-1" style={{ padding: '0.5rem 1rem' }}>
                <div className="flex gap-1" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
                  {Object.entries(taskSummary).map(([status, count]) => (
                    <span key={status} className={`badge ${status}`}>{count} {status}</span>
                  ))}
                  {(tasks ?? []).length === 0 && <span className="text-dim text-sm">No tasks yet</span>}
                  <div style={{ marginLeft: 'auto' }} className="flex gap-1">
                    {activeProject?.status === 'ready' && (
                      <button className="btn btn-primary" style={{ padding: '2px 12px' }}
                        onClick={handleStartExecution}>Start</button>
                    )}
                    {activeProject?.status === 'executing' && (
                      <button className="btn btn-secondary" style={{ padding: '2px 12px' }}
                        onClick={handlePause}>Pause</button>
                    )}
                    {activeProject?.status === 'paused' && (
                      <button className="btn btn-primary" style={{ padding: '2px 12px' }}
                        onClick={handleStartExecution}>Resume</button>
                    )}
                  </div>
                </div>
              </div>

              {/* Chat area */}
              <div style={{ flex: 1, overflow: 'auto', marginBottom: '0.5rem' }}>
                {messages.map((msg, i) => (
                  <div key={i} style={{
                    padding: '0.5rem 0.75rem',
                    marginBottom: '0.25rem',
                    borderRadius: 6,
                    background: msg.role === 'user' ? 'var(--surface-hover, rgba(100,150,255,0.1))'
                      : msg.role === 'system' ? 'transparent'
                      : 'var(--surface, rgba(255,255,255,0.03))',
                    borderLeft: msg.role === 'system' ? '2px solid var(--text-dim, #666)' : 'none',
                    fontSize: msg.role === 'system' ? '0.85rem' : '0.95rem',
                    color: msg.role === 'system' ? 'var(--text-dim, #888)' : 'inherit',
                  }}>
                    {msg.role === 'user' && <strong>You: </strong>}
                    {msg.content}
                  </div>
                ))}
                <div ref={chatEndRef} />
              </div>

              {/* Chat input */}
              <div className="flex gap-1">
                <input style={{ flex: 1 }} value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleSend()}
                  placeholder="Direct your game... (e.g., 'add an enemy that chases the player')"
                  disabled={chatLoading} />
                <button className="btn btn-primary" onClick={handleSend}
                  disabled={chatLoading || !input.trim()}>Send</button>
              </div>
            </>
          )}
        </div>

        {/* Knowledge sidebar */}
        {showKnowledge && (
          <div style={{ width: 300, overflow: 'auto', borderLeft: '1px solid var(--border, #333)', paddingLeft: '1rem' }}>
            <h3 style={{ marginBottom: '0.5rem' }}>NoZ Knowledge</h3>
            {knowledgeNodes.length === 0 ? (
              <span className="text-dim text-sm">Loading...</span>
            ) : (
              knowledgeNodes.map(node => (
                <div key={node.id}>
                  <div style={{ cursor: 'pointer', padding: '4px 0', fontSize: '0.85rem' }}
                    onClick={() => toggleNodeExpand(node.id)}>
                    {expandedNodes[node.id] ? '▼' : '▶'} {node.name}
                  </div>
                  {expandedNodes[node.id] && (
                    <div style={{ paddingLeft: '1rem' }}>
                      {expandedNodes[node.id].map(child => (
                        <div key={child.id} style={{ padding: '2px 0', fontSize: '0.8rem' }}
                          className="text-dim">
                          {child.name}
                        </div>
                      ))}
                      {expandedNodes[node.id].length === 0 && (
                        <span className="text-dim text-sm">No children</span>
                      )}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// --- Helpers ---

function formatSSEEvent(event: SSEEvent): string | null {
  switch (event.type) {
    case 'task_start':
      return `Task started: ${event.message || event.task_id?.slice(0, 8)}`
    case 'task_complete':
      return `Task completed: ${event.message || event.task_id?.slice(0, 8)}`
    case 'task_failed':
      return `Task failed: ${event.message || event.task_id?.slice(0, 8)}`
    case 'project_complete':
      return 'All tasks finished!'
    case 'project_failed':
      return `Project finished with failures: ${event.message}`
    case 'wave_checkpoint':
      return `Wave complete: ${event.message}`
    case 'budget_warning':
      return `Budget warning: ${event.message}`
    default:
      return null
  }
}
