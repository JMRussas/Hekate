// Orchestration Engine - Usage API

import { apiFetch } from './client'
import type { BudgetStatus, UsageSummary } from '../types'

export const getUsageSummary = (projectId?: string) =>
  apiFetch<UsageSummary>(`/usage/summary${projectId ? `?project_id=${projectId}` : ''}`)

export const getBudget = () =>
  apiFetch<BudgetStatus>('/usage/budget')

export const getDailyUsage = (days = 30) =>
  apiFetch<{ date: string; cost_usd: number; api_calls: number }[]>(`/usage/daily?days=${days}`)

export const getUsageByProject = () =>
  apiFetch<{ project_id: string; project_name: string; cost_usd: number }[]>('/usage/by-project')
