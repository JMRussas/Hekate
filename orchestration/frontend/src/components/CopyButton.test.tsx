// Orchestration Engine - CopyButton Tests

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CopyButton from './CopyButton'

const mockCopy = vi.fn().mockResolvedValue(true)

vi.mock('../hooks/useClipboard', () => ({
  useClipboard: () => ({ copied: false, copy: mockCopy }),
}))

describe('CopyButton', () => {
  it('renders with default label', () => {
    render(<CopyButton text="hello" />)
    expect(screen.getByText('Copy')).toBeInTheDocument()
  })

  it('renders with custom label', () => {
    render(<CopyButton text="hello" label="Copy Output" />)
    expect(screen.getByText('Copy Output')).toBeInTheDocument()
  })

  it('calls copy with text on click', () => {
    render(<CopyButton text="hello" />)
    fireEvent.click(screen.getByText('Copy'))
    expect(mockCopy).toHaveBeenCalledWith('hello')
  })
})
