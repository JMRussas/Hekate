// Orchestration Engine - Layout Component
//
// Depends on: hooks/useAuth.tsx, hooks/useTheme.ts
// Used by:    App.tsx

import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'

export default function Layout() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()

  return (
    <div className="layout">
      <nav className="sidebar" aria-label="Main navigation">
        <h1>Orchestration</h1>
        <NavLink to="/" className={({ isActive }) => isActive ? 'active' : ''} end>
          Dashboard
        </NavLink>
        <NavLink to="/usage" className={({ isActive }) => isActive ? 'active' : ''}>
          Usage & Budget
        </NavLink>
        <NavLink to="/services" className={({ isActive }) => isActive ? 'active' : ''}>
          Services
        </NavLink>
        <NavLink to="/rag" className={({ isActive }) => isActive ? 'active' : ''}>
          RAG Databases
        </NavLink>
        {user?.role === 'admin' && (
          <>
            <NavLink to="/admin" className={({ isActive }) => isActive ? 'active' : ''}>
              Admin
            </NavLink>
            <NavLink to="/analytics" className={({ isActive }) => isActive ? 'active' : ''}>
              Analytics
            </NavLink>
          </>
        )}
        {user && (
          <div className="sidebar-user">
            <span className="user-name">{user.display_name}</span>
            <div className="flex gap-1">
              <button className="logout-btn" onClick={toggle}>
                {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
              </button>
              <button className="logout-btn" onClick={logout}>Sign Out</button>
            </div>
          </div>
        )}
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
