// Orchestration Engine - Checkpoint Form
//
// Schema-driven checkpoint resolution. When a checkpoint has schema_json,
// renders an @rjsf/core Form for structured input. Falls back to a plain
// textarea for free-text guidance when no schema is present.
// Action buttons (retry/skip/fail) are always shown below the form.
//
// Depends on: @rjsf/core, @rjsf/utils, @rjsf/validator-ajv8, types
// Used by:    pages/ProjectDetail.tsx

import { useState } from 'react'
import Form from '@rjsf/core'
import type { IChangeEvent } from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import type { RJSFSchema } from '@rjsf/utils'
import type { Checkpoint } from '../types'

interface CheckpointFormProps {
  checkpoint: Checkpoint
  loading: boolean
  onResolve: (action: string, guidance: string, structured_response?: Record<string, unknown>) => void
  onCancel: () => void
}

export default function CheckpointForm({ checkpoint, loading, onResolve, onCancel }: CheckpointFormProps) {
  const [guidance, setGuidance] = useState('')
  const [formData, setFormData] = useState<Record<string, unknown>>({})
  const [validationError, setValidationError] = useState('')

  const hasSchema = checkpoint.schema_json != null

  const handleAction = (action: string) => {
    if (hasSchema) {
      onResolve(action, '', formData)
    } else {
      onResolve(action, guidance)
    }
  }

  // rjsf onSubmit fires only when validation passes — use it for the retry action
  const handleSchemaSubmit = (data: IChangeEvent) => {
    setValidationError('')
    setFormData(data.formData ?? {})
    onResolve('retry', '', data.formData ?? {})
  }

  const handleSchemaError = () => {
    setValidationError('Please fix the validation errors above.')
  }

  return (
    <div>
      {validationError && (
        <div className="text-sm mb-1" style={{ color: 'var(--error)' }}>{validationError}</div>
      )}

      {hasSchema ? (
        <div className="mb-1">
          <Form
            schema={checkpoint.schema_json as RJSFSchema}
            formData={formData}
            onChange={(e: IChangeEvent) => {
              setFormData(e.formData ?? {})
              setValidationError('')
            }}
            onSubmit={handleSchemaSubmit}
            onError={handleSchemaError}
            validator={validator}
            uiSchema={{ 'ui:submitButtonOptions': { norender: true } }}
            liveValidate={false}
          />
        </div>
      ) : (
        <div className="form-group">
          <textarea
            value={guidance}
            onChange={e => setGuidance(e.target.value)}
            placeholder="Optional guidance..."
            style={{ minHeight: '50px' }}
          />
        </div>
      )}

      <div className="flex gap-1">
        <button
          className="btn btn-primary btn-sm"
          onClick={() => handleAction('retry')}
          disabled={loading}
        >
          {loading ? '...' : 'Retry'}
        </button>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => handleAction('skip')}
          disabled={loading}
        >
          Skip
        </button>
        <button
          className="btn btn-danger btn-sm"
          onClick={() => handleAction('fail')}
          disabled={loading}
        >
          Fail
        </button>
        <button
          className="btn btn-sm"
          style={{ background: 'transparent', color: 'var(--text-dim)' }}
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
