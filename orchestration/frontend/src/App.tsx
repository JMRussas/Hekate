// Orchestration Engine - App Router
//
// Depends on: hooks/useAuth.tsx, components/AuthGuard.tsx, components/RequireRole.tsx,
//             components/Layout.tsx, components/ErrorBoundary.tsx
// Used by:    main.tsx

import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './hooks/useAuth'
import AuthGuard from './components/AuthGuard'
import RequireRole from './components/RequireRole'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import Login from './pages/Login'
import Register from './pages/Register'
import OIDCCallback from './pages/OIDCCallback'
import Dashboard from './pages/Dashboard'
import ProjectDetail from './pages/ProjectDetail'
import TaskDetail from './pages/TaskDetail'
import Usage from './pages/Usage'
import Services from './pages/Services'
import Admin from './pages/Admin'
import Analytics from './pages/Analytics'
import RAG from './pages/RAG'
import NotFound from './pages/NotFound'

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            {/* Public routes */}
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/auth/oidc/callback" element={<OIDCCallback />} />

            {/* Protected routes */}
            <Route element={<AuthGuard />}>
              <Route element={<Layout />}>
                <Route path="/" element={<Dashboard />} />
                <Route path="/project/:id" element={<ProjectDetail />} />
                <Route path="/project/:id/task/:taskId" element={<TaskDetail />} />
                <Route path="/usage" element={<Usage />} />
                <Route path="/services" element={<Services />} />
                <Route path="/rag" element={<RAG />} />
                {/* Admin-only routes */}
                <Route element={<RequireRole role="admin" />}>
                  <Route path="/admin" element={<Admin />} />
                  <Route path="/analytics" element={<Analytics />} />
                </Route>
              </Route>
            </Route>

            {/* 404 catch-all */}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
