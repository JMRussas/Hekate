// Orchestration Engine - Role Guard Component
//
// Route-level guard that restricts access by user role.
// Renders <Outlet /> for authorized users, denial message otherwise.
//
// Depends on: hooks/useAuth.tsx
// Used by:    App.tsx

import { Outlet } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

interface RequireRoleProps {
  role: string
  fallback?: React.ReactNode
}

export default function RequireRole({ role, fallback }: RequireRoleProps) {
  const { user } = useAuth()

  if (!user || user.role !== role) {
    return fallback ? <>{fallback}</> : (
      <p className="text-dim" style={{ padding: '2rem' }}>
        Access denied. {role} role required.
      </p>
    )
  }

  return <Outlet />
}
