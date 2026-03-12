import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// Mock all page components to avoid deep dependency chains
vi.mock('./pages/Login', () => ({ default: () => <div>Login Page</div> }))
vi.mock('./pages/Register', () => ({ default: () => <div>Register Page</div> }))
vi.mock('./pages/Dashboard', () => ({ default: () => <div>Dashboard Page</div> }))
vi.mock('./pages/ProjectDetail', () => ({ default: () => <div>Project Detail</div> }))
vi.mock('./pages/TaskDetail', () => ({ default: () => <div>Task Detail</div> }))
vi.mock('./pages/Usage', () => ({ default: () => <div>Usage Page</div> }))
vi.mock('./pages/Services', () => ({ default: () => <div>Services Page</div> }))
vi.mock('./pages/NotFound', () => ({ default: () => <div>Not Found Page</div> }))

// Mock auth to skip token loading
vi.mock('./api/auth', () => ({
  getAccessToken: vi.fn(() => null),
  apiLogin: vi.fn(),
  apiRefresh: vi.fn(),
  apiGetMe: vi.fn(),
  setTokens: vi.fn(),
  clearTokens: vi.fn(),
}))

import App from './App'

// Override window.location for BrowserRouter
function renderApp(route = '/') {
  window.history.pushState({}, '', route)
  return render(<App />)
}

describe('App', () => {
  it('renders without crash', () => {
    renderApp()
    // App renders — no crash. The auth guard will redirect to /login
    // since we mocked getAccessToken to return null.
  })

  it('/login renders Login page', async () => {
    renderApp('/login')
    await waitFor(() => {
      expect(screen.getByText('Login Page')).toBeInTheDocument()
    })
  })

  it('unknown route renders NotFound', async () => {
    renderApp('/nonexistent-page')
    await waitFor(() => {
      expect(screen.getByText('Not Found Page')).toBeInTheDocument()
    })
  })
})
