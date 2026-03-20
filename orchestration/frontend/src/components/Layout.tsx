// Hekate - Layout

import { NavLink, Outlet } from 'react-router-dom'

export default function Layout() {
  return (
    <div className="layout">
      <nav className="sidebar" aria-label="Main navigation">
        <h1>Hekate</h1>
        <NavLink to="/" className={({ isActive }) => isActive ? 'active' : ''} end>
          Projects
        </NavLink>
        <NavLink to="/services" className={({ isActive }) => isActive ? 'active' : ''}>
          Services
        </NavLink>
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
