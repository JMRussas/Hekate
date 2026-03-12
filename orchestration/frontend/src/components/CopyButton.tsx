// Orchestration Engine - Copy Button Component
//
// Reusable button that copies text to clipboard with visual feedback.
//
// Depends on: hooks/useClipboard.ts
// Used by:    pages/TaskDetail.tsx

import { useClipboard } from '../hooks/useClipboard'

interface CopyButtonProps {
  text: string
  label?: string
  className?: string
}

export default function CopyButton({ text, label = 'Copy', className = '' }: CopyButtonProps) {
  const { copied, copy } = useClipboard()

  return (
    <button
      className={`btn btn-sm btn-secondary copy-btn ${className}`}
      onClick={() => copy(text)}
      title={copied ? 'Copied!' : `Copy ${label}`}
    >
      {copied ? 'Copied!' : label}
    </button>
  )
}
