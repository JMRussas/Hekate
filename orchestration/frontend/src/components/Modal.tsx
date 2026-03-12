// Orchestration Engine - Modal Component
//
// Portal-based modal with backdrop click, Escape key, focus trap,
// and ARIA attributes for accessibility.
//
// Depends on: (none)
// Used by:    pages/TaskDetail.tsx

import { useEffect, useRef, useId } from 'react'
import { createPortal } from 'react-dom'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  children: React.ReactNode
}

export default function Modal({ open, onClose, title, children }: ModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const previousFocusRef = useRef<Element | null>(null)
  const titleId = useId()

  // Save/restore focus and lock body scroll
  useEffect(() => {
    if (!open) return
    previousFocusRef.current = document.activeElement
    document.body.style.overflow = 'hidden'

    // Focus the modal content for keyboard users
    requestAnimationFrame(() => {
      contentRef.current?.focus()
    })

    return () => {
      document.body.style.overflow = '';
      (previousFocusRef.current as HTMLElement)?.focus?.()
    }
  }, [open])

  // Escape key
  useEffect(() => {
    if (!open) return
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [open, onClose])

  // Focus trap: Tab cycles within modal
  useEffect(() => {
    if (!open) return
    const handleTab = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || !contentRef.current) return
      const focusable = contentRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handleTab)
    return () => window.removeEventListener('keydown', handleTab)
  }, [open])

  if (!open) return null

  return createPortal(
    <div
      className="modal-overlay"
      ref={overlayRef}
      onClick={e => { if (e.target === overlayRef.current) onClose() }}
    >
      <div
        className="modal-content"
        ref={contentRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="flex-between mb-2">
          <h3 id={titleId} style={{ margin: 0 }}>{title}</h3>
          <button
            className="btn btn-sm"
            onClick={onClose}
            aria-label="Close"
            style={{ background: 'transparent', color: 'var(--text-dim)' }}
          >
            &times;
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  )
}
