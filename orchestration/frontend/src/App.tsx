// Hekate - App Router
//
// Stripped to core: projects, tasks, services, events.
// No auth, no admin, no analytics, no RAG.

import { BrowserRouter, Routes, Route } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import ProjectDetail from './pages/ProjectDetail'
import TaskDetail from './pages/TaskDetail'
import Services from './pages/Services'
import Observatory from './pages/Observatory'
import NotFound from './pages/NotFound'

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/project/:id" element={<ProjectDetail />} />
            <Route path="/project/:id/task/:taskId" element={<TaskDetail />} />
            <Route path="/project/:id/observatory" element={<Observatory />} />
            <Route path="/services" element={<Services />} />
          </Route>
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
