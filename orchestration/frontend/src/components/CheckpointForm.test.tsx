// Orchestration Engine - CheckpointForm Tests

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CheckpointForm from './CheckpointForm'
import type { Checkpoint } from '../types'

function makeCheckpoint(overrides: Partial<Checkpoint> = {}): Checkpoint {
  return {
    id: 'cp-1',
    project_id: 'proj-1',
    task_id: 'task-1',
    checkpoint_type: 'task_checkpoint',
    summary: 'Test checkpoint',
    attempts: [],
    question: 'What should we do?',
    response: null,
    resolved_at: null,
    schema_json: null,
    created_at: Date.now(),
    ...overrides,
  }
}

describe('CheckpointForm', () => {
  it('renders textarea when schema_json is null', () => {
    const onResolve = vi.fn()
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint()}
        loading={false}
        onResolve={onResolve}
        onCancel={vi.fn()}
      />
    )
    expect(screen.getByPlaceholderText('Optional guidance...')).toBeInTheDocument()
    expect(screen.getByText('Retry')).toBeInTheDocument()
    expect(screen.getByText('Skip')).toBeInTheDocument()
    expect(screen.getByText('Fail')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('renders dynamic form when schema_json is provided', () => {
    const schema = {
      type: 'object',
      properties: {
        fix_description: { type: 'string', title: 'Fix Description' },
      },
    }
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint({ schema_json: schema })}
        loading={false}
        onResolve={vi.fn()}
        onCancel={vi.fn()}
      />
    )
    // rjsf renders the field label
    expect(screen.getByText('Fix Description')).toBeInTheDocument()
    // textarea should NOT be present
    expect(screen.queryByPlaceholderText('Optional guidance...')).not.toBeInTheDocument()
  })

  it('submit calls onResolve with guidance for free-text form', async () => {
    const onResolve = vi.fn()
    const user = userEvent.setup()
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint()}
        loading={false}
        onResolve={onResolve}
        onCancel={vi.fn()}
      />
    )
    const textarea = screen.getByPlaceholderText('Optional guidance...')
    await user.type(textarea, 'try a different approach')
    fireEvent.click(screen.getByText('Retry'))
    expect(onResolve).toHaveBeenCalledWith('retry', 'try a different approach')
  })

  it('submit calls onResolve with structured data for schema form', () => {
    const onResolve = vi.fn()
    const schema = {
      type: 'object',
      properties: {
        value: { type: 'string', title: 'Value', default: 'hello' },
      },
    }
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint({ schema_json: schema })}
        loading={false}
        onResolve={onResolve}
        onCancel={vi.fn()}
      />
    )
    // Click retry — with schema, onResolve gets empty string for guidance + formData
    fireEvent.click(screen.getByText('Retry'))
    expect(onResolve).toHaveBeenCalledWith('retry', '', expect.any(Object))
  })

  it('renders approve/reject form for APPROVE_REJECT schema', () => {
    const schema = {
      type: 'object',
      properties: {
        approved: { type: 'boolean', title: 'Approved' },
        reason: { type: 'string', title: 'Reason' },
      },
    }
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint({ schema_json: schema })}
        loading={false}
        onResolve={vi.fn()}
        onCancel={vi.fn()}
      />
    )
    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.getByText('Reason')).toBeInTheDocument()
  })

  it('disables buttons when loading', () => {
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint()}
        loading={true}
        onResolve={vi.fn()}
        onCancel={vi.fn()}
      />
    )
    expect(screen.getByText('...')).toBeDisabled()
    expect(screen.getByText('Skip')).toBeDisabled()
    expect(screen.getByText('Fail')).toBeDisabled()
  })

  it('calls onCancel when cancel clicked', () => {
    const onCancel = vi.fn()
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint()}
        loading={false}
        onResolve={vi.fn()}
        onCancel={onCancel}
      />
    )
    fireEvent.click(screen.getByText('Cancel'))
    expect(onCancel).toHaveBeenCalled()
  })

  it('skip action passes guidance for free-text form', async () => {
    const onResolve = vi.fn()
    const user = userEvent.setup()
    render(
      <CheckpointForm
        checkpoint={makeCheckpoint()}
        loading={false}
        onResolve={onResolve}
        onCancel={vi.fn()}
      />
    )
    await user.type(screen.getByPlaceholderText('Optional guidance...'), 'skip reason')
    fireEvent.click(screen.getByText('Skip'))
    expect(onResolve).toHaveBeenCalledWith('skip', 'skip reason')
  })
})
