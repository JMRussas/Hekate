// Orchestration Engine - Analytics API
//
// Admin-only analytics endpoints.
//
// Depends on: api/client.ts
// Used by:    pages/Analytics.tsx

import { apiFetch } from './client'

// Cost Breakdown
export interface CostByProject {
  project_id: string
  project_name: string
  cost_usd: number
  task_count: number
}

export interface CostByModelTier {
  model_tier: string
  cost_usd: number
  task_count: number
  avg_cost_per_task: number
}

export interface DailyCostTrend {
  date: string
  cost_usd: number
  api_calls: number
}

export interface CostBreakdown {
  by_project: CostByProject[]
  by_model_tier: CostByModelTier[]
  daily_trend: DailyCostTrend[]
  total_cost_usd: number
}

// Task Outcomes
export interface TaskOutcomeByTier {
  model_tier: string
  total: number
  completed: number
  failed: number
  needs_review: number
  success_rate: number
}

export interface VerificationByTier {
  model_tier: string
  total_verified: number
  passed: number
  gaps_found: number
  human_needed: number
  pass_rate: number
}

export interface TaskOutcomes {
  by_tier: TaskOutcomeByTier[]
  verification_by_tier: VerificationByTier[]
}

// Efficiency
export interface RetryByTier {
  model_tier: string
  total_tasks: number
  tasks_with_retries: number
  total_retries: number
  retry_rate: number
}

export interface WaveThroughput {
  project_id: string
  project_name: string
  wave: number
  task_count: number
  avg_duration_seconds: number | null
}

export interface CostEfficiencyItem {
  model_tier: string
  cost_usd: number
  tasks_completed: number
  verification_pass_count: number
  cost_per_pass: number | null
}

export interface Efficiency {
  retries_by_tier: RetryByTier[]
  checkpoint_count: number
  unresolved_checkpoint_count: number
  wave_throughput: WaveThroughput[]
  cost_efficiency: CostEfficiencyItem[]
}

// API Functions
export const getCostBreakdown = (days = 30) =>
  apiFetch<CostBreakdown>(`/admin/analytics/cost-breakdown?days=${days}`)

export const getTaskOutcomes = () =>
  apiFetch<TaskOutcomes>('/admin/analytics/task-outcomes')

export const getEfficiency = () =>
  apiFetch<Efficiency>('/admin/analytics/efficiency')
