// Orchestration Engine - Auth Guard
//
// Redirects unauthenticated users to /login.
//
// Depends on: hooks/useAuth.tsx
// Used by:    App.tsx

import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

export default function AuthGuard() {
  const { user, loading } = useAuth()

  if (loading) {
    return <div className="auth-loading">Loading...</div>
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <Outlet />
}
