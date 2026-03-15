// Orchestration Engine - Projects API

import { apiFetch, apiPost, apiPatch, apiDelete, authFetch } from './client'
import type { Project, Plan, Task, Checkpoint, CoverageReport, PlanningRigor, GitStatus } from '../types'

export const listProjects = (status?: string) =>
  apiFetch<Project[]>(`/projects${status ? `?status=${status}` : ''}`)

export const getProject = (id: string) =>
  apiFetch<Project>(`/projects/${id}`)

export interface CreateProjectOptions {
  name: string
  requirements: string
  planning_rigor?: PlanningRigor
  repo_path?: string
  config?: Record<string, unknown>
}

export const createProject = (opts: CreateProjectOptions) =>
  apiPost<Project>('/projects', {
    name: opts.name,
    requirements: opts.requirements,
    planning_rigor: opts.planning_rigor ?? 'L2',
    repo_path: opts.repo_path || undefined,
    config: opts.config ?? {},
  })

export const updateProject = (id: string, body: Record<string, unknown>) =>
  apiPatch<Project>(`/projects/${id}`, body)

export const deleteProject = (id: string) =>
  apiDelete(`/projects/${id}`)

export const generatePlan = (projectId: string) =>
  apiPost<{ plan_id: string; plan: unknown; cost_usd: number }>(`/projects/${projectId}/plan`)

export const listPlans = (projectId: string) =>
  apiFetch<Plan[]>(`/projects/${projectId}/plans`)

export const approvePlan = (projectId: string, planId: string) =>
  apiPost<{ tasks_created: number; estimated_cost_usd: number }>(`/projects/${projectId}/plans/${planId}/approve`)

export const startExecution = (projectId: string) =>
  apiPost<{ status: string }>(`/projects/${projectId}/execute`)

export const pauseExecution = (projectId: string) =>
  apiPost<{ status: string }>(`/projects/${projectId}/pause`)

export const cancelProject = (projectId: string) =>
  apiPost<{ status: string }>(`/projects/${projectId}/cancel`)

export interface TaskListParams {
  status?: string
  wave?: number
  phase?: string
  model_tier?: string
  search?: string
  sort?: 'priority' | 'created_at' | 'wave' | 'status'
  sort_dir?: 'asc' | 'desc'
  exclude_output?: boolean
}

export const listTasks = (projectId: string, params?: TaskListParams) => {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.wave !== undefined) qs.set('wave', String(params.wave))
  if (params?.phase) qs.set('phase', params.phase)
  if (params?.model_tier) qs.set('model_tier', params.model_tier)
  if (params?.search) qs.set('search', params.search)
  if (params?.sort) qs.set('sort', params.sort)
  if (params?.sort_dir) qs.set('sort_dir', params.sort_dir)
  if (params?.exclude_output) qs.set('exclude_output', 'true')
  const query = qs.toString()
  return apiFetch<Task[]>(`/tasks/project/${projectId}${query ? `?${query}` : ''}`)
}

export const fetchCoverage = (projectId: string) =>
  apiFetch<CoverageReport>(`/projects/${projectId}/coverage`)

export const fetchCheckpoints = (projectId: string, resolved = false) =>
  apiFetch<Checkpoint[]>(`/checkpoints/project/${projectId}${resolved ? '?resolved=true' : ''}`)

export const resolveCheckpoint = (checkpointId: string, action: string, guidance = '') =>
  apiPost<Checkpoint>(`/checkpoints/${checkpointId}/resolve`, { action, guidance })

export const reviewTask = (taskId: string, action: string, feedback = '') =>
  apiPost<Task>(`/tasks/${taskId}/review`, { action, feedback })

export const updateTask = (taskId: string, body: Record<string, unknown>) =>
  apiPatch<Task>(`/tasks/${taskId}`, body)

export const retryTask = (taskId: string) =>
  apiPost<Task>(`/tasks/${taskId}/retry`)

export const expandEpic = (taskId: string) =>
  apiPost<{ project_id: string; name: string }>(`/tasks/${taskId}/expand`)

export const cloneProject = (projectId: string) =>
  apiPost<Project>(`/projects/${projectId}/clone`)

export const exportProject = async (projectId: string) => {
  const resp = await authFetch(`/projects/${projectId}/export`)
  if (!resp.ok) throw new Error('Export failed')
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `project_${projectId}.json`
  a.click()
  URL.revokeObjectURL(url)
}

export const bulkTaskAction = (action: 'retry' | 'cancel', taskIds: string[]) =>
  apiPost<{ succeeded: string[]; failed: { id: string; reason: string }[] }>(
    '/tasks/bulk', { action, task_ids: taskIds }
  )

export const fetchGitStatus = (projectId: string) =>
  apiFetch<GitStatus | null>(`/projects/${projectId}/git-status`)
