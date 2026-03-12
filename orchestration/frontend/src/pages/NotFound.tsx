// Orchestration Engine - 404 Page
//
// Catch-all route for unknown paths.
//
// Depends on: react-router-dom
// Used by:    App.tsx

import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div style={{ padding: '2rem', maxWidth: 600, margin: '4rem auto', textAlign: 'center' }}>
      <h1 style={{ fontSize: '4rem', marginBottom: '0.5rem' }}>404</h1>
      <p style={{ color: 'var(--text-muted, #888)', marginBottom: '2rem' }}>
        Page not found
      </p>
      <Link to="/" className="btn">Back to Dashboard</Link>
    </div>
  )
}
