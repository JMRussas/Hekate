import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../api/services', () => ({
  listServices: vi.fn(),
}))

import { listServices } from '../api/services'
import Services from './Services'

const mockListServices = vi.mocked(listServices)

beforeEach(() => {
  vi.resetAllMocks()
})

describe('Services', () => {
  it('shows loading spinner initially', () => {
    mockListServices.mockReturnValue(new Promise(() => {}))
    render(<Services />)
    expect(screen.getByText('Loading services...')).toBeInTheDocument()
  })

  it('shows services after load', async () => {
    mockListServices.mockResolvedValueOnce([
      { id: 's1', name: 'Ollama (local)', status: 'online', category: 'LLM', method: 'HTTP', details: {} },
      { id: 's2', name: 'Claude API', status: 'offline', category: 'LLM', method: 'HTTP', details: {} },
    ])

    render(<Services />)
    expect(await screen.findByText('Ollama (local)')).toBeInTheDocument()
    expect(screen.getByText('Claude API')).toBeInTheDocument()
  })

  it('shows error on failure', async () => {
    mockListServices.mockRejectedValueOnce(new Error('Network error'))

    render(<Services />)
    expect(await screen.findByText(/Failed to load services: .*Network error/)).toBeInTheDocument()
  })

  it('refresh button calls API with refresh=true', async () => {
    mockListServices.mockResolvedValue([])
    render(<Services />)

    await waitFor(() => expect(mockListServices).toHaveBeenCalledTimes(1))
    expect(mockListServices).toHaveBeenCalledWith()

    const user = userEvent.setup()
    await user.click(screen.getByText('Refresh'))

    await waitFor(() => expect(mockListServices).toHaveBeenCalledWith(true))
  })

  it('refresh button shows Checking while refreshing', async () => {
    mockListServices.mockResolvedValueOnce([])
    render(<Services />)
    await waitFor(() => expect(mockListServices).toHaveBeenCalledTimes(1))

    // Make the refresh call hang
    mockListServices.mockImplementation(() => new Promise(() => {}))

    const user = userEvent.setup()
    await user.click(screen.getByText('Refresh'))

    expect(screen.getByText('Checking...')).toBeInTheDocument()
  })

  it('shows service status badge', async () => {
    mockListServices.mockResolvedValueOnce([
      { id: 's1', name: 'Ollama', status: 'online', category: 'LLM', details: {} },
    ])

    render(<Services />)
    const badge = await screen.findByText('online')
    expect(badge).toHaveClass('online')
  })

  it('shows details pre block when non-empty', async () => {
    mockListServices.mockResolvedValueOnce([
      { id: 's1', name: 'Ollama', status: 'online', category: 'LLM', details: { version: '1.0' } },
    ])

    render(<Services />)
    await waitFor(() => {
      expect(screen.getByText(/"version": "1.0"/)).toBeInTheDocument()
    })
  })
})
