// Orchestration Engine - Services API

import { apiFetch } from './client'
import type { Resource } from '../types'

export const listServices = (refresh = false) =>
  apiFetch<Resource[]>(`/services${refresh ? '?refresh=true' : ''}`)
