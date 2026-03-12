// Orchestration Engine - RAG API
//
// Read-only endpoints for inspecting RAG databases.
//
// Depends on: api/client.ts
// Used by:    pages/RAG.tsx

import { apiFetch } from './client'

export interface RAGDatabaseInfo {
  name: string
  path: string
  exists: boolean
  file_size_bytes: number
  chunk_count: number
  source_count: number
  index_status: string
  sources: { source: string; count: number }[]
}

export interface RAGChunkPreview {
  id: string
  source: string
  type_name: string | null
  file_path: string | null
  text_preview: string
}

export interface RAGDocumentsResponse {
  total: number
  items: RAGChunkPreview[]
}

export const listDatabases = () =>
  apiFetch<RAGDatabaseInfo[]>('/rag/databases')

export const listSources = (name: string) =>
  apiFetch<{ source: string; count: number }[]>(`/rag/databases/${name}/sources`)

export const listDocuments = (name: string, params?: { source?: string; offset?: number; limit?: number }) => {
  const qs = new URLSearchParams()
  if (params?.source) qs.set('source', params.source)
  if (params?.offset !== undefined) qs.set('offset', String(params.offset))
  if (params?.limit !== undefined) qs.set('limit', String(params.limit))
  const suffix = qs.toString() ? `?${qs}` : ''
  return apiFetch<RAGDocumentsResponse>(`/rag/databases/${name}/documents${suffix}`)
}
