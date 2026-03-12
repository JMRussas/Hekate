// Orchestration Engine - Error Boundary
//
// Catches unhandled React errors and shows a fallback UI
// instead of a blank white screen.
//
// Depends on: (none)
// Used by:    App.tsx

import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '2rem', maxWidth: 600, margin: '4rem auto' }}>
          <h1>Something went wrong</h1>
          <p style={{ color: 'var(--text-muted, #888)', marginTop: '1rem' }}>
            An unexpected error occurred. Try refreshing the page.
          </p>
          {this.state.error && (
            <pre style={{
              marginTop: '1rem',
              padding: '1rem',
              background: 'var(--bg-secondary, #1a1a2e)',
              borderRadius: 8,
              overflow: 'auto',
              fontSize: '0.85rem',
            }}>
              {this.state.error.message}
            </pre>
          )}
          <button
            onClick={() => window.location.reload()}
            style={{ marginTop: '1.5rem' }}
            className="btn"
          >
            Reload page
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
