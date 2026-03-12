// Orchestration Engine - Admin API
//
// Admin-only endpoints: user management, system stats.
//
// Depends on: api/client.ts
// Used by:    pages/Admin.tsx

import { apiFetch, apiPatch } from './client'

export interface AdminUser {
  id: string
  email: string
  display_name: string
  role: string
  is_active: boolean
  created_at: number
  last_login_at: number | null
  project_count: number
}

export interface AdminUserUpdate {
  role?: 'admin' | 'user'
  is_active?: boolean
}

export interface AdminStats {
  total_users: number
  active_users: number
  total_projects: number
  projects_by_status: Record<string, number>
  total_tasks: number
  tasks_by_status: Record<string, number>
  total_spend_usd: number
  spend_by_model: Record<string, number>
  task_completion_rate: number
}

export const listUsers = () =>
  apiFetch<AdminUser[]>('/admin/users')

export const updateUser = (userId: string, body: AdminUserUpdate) =>
  apiPatch<AdminUser>(`/admin/users/${userId}`, body)

export const getStats = () =>
  apiFetch<AdminStats>('/admin/stats')
