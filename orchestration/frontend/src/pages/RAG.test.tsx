// Orchestration Engine - RAG Page Tests

import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import RAG from './RAG'

vi.mock('../api/rag', () => ({
  listDatabases: vi.fn().mockResolvedValue([
    {
      name: 'noz', path: '/data/noz.db', exists: true,
      file_size_bytes: 1048576, chunk_count: 500, source_count: 3,
      index_status: 'loaded',
      sources: [
        { source: 'engine', count: 300 },
        { source: 'game', count: 150 },
        { source: 'docs', count: 50 },
      ],
    },
    {
      name: 'verse', path: '/data/verse.db', exists: false,
      file_size_bytes: 0, chunk_count: 0, source_count: 0,
      index_status: 'unknown', sources: [],
    },
  ]),
  listDocuments: vi.fn().mockResolvedValue({
    total: 0, items: [],
  }),
}))

function renderRAG() {
  return render(
    <BrowserRouter>
      <RAG />
    </BrowserRouter>
  )
}

describe('RAG', () => {
  it('renders heading', () => {
    renderRAG()
    expect(screen.getByText('RAG Databases')).toBeInTheDocument()
  })

  it('shows database cards after loading', async () => {
    renderRAG()
    await waitFor(() => {
      expect(screen.getByText('noz')).toBeInTheDocument()
    })
    expect(screen.getByText('verse')).toBeInTheDocument()
  })

  it('shows chunk count for existing database', async () => {
    renderRAG()
    await waitFor(() => {
      expect(screen.getByText('500 chunks')).toBeInTheDocument()
    })
  })

  it('shows missing badge for non-existent database', async () => {
    renderRAG()
    await waitFor(() => {
      expect(screen.getByText('missing')).toBeInTheDocument()
    })
  })
})
