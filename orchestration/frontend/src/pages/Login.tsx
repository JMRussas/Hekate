// Orchestration Engine - Login Page
//
// Login form with link to register.
//
// Depends on: hooks/useAuth.tsx
// Used by:    App.tsx

import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { fetchOIDCProviders, type OIDCProvider } from '../api/auth'
import { useAuth } from '../hooks/useAuth'

export default function Login() {
  const { login, loginWithOIDC } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [providers, setProviders] = useState<OIDCProvider[]>([])

  useEffect(() => { fetchOIDCProviders().then(setProviders) }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(email, password)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h1>Orchestration Engine</h1>
        <h2>Sign In</h2>
        {error && <div className="auth-error">{error}</div>}
        <form onSubmit={handleSubmit}>
          <label htmlFor="login-email">
            Email
            <input
              id="login-email"
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              required
              autoFocus
            />
          </label>
          <label htmlFor="login-password">
            Password
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              minLength={8}
            />
          </label>
          <button type="submit" disabled={loading}>
            {loading ? 'Signing in...' : 'Sign In'}
          </button>
        </form>
        {providers.length > 0 && (
          <>
            <div className="auth-divider"><span>or</span></div>
            <div className="oauth-buttons">
              {providers.map(p => (
                <button
                  key={p.name}
                  type="button"
                  className="oauth-btn"
                  onClick={() => loginWithOIDC(p.name)}
                  disabled={loading}
                >
                  Continue with {p.display_name}
                </button>
              ))}
            </div>
          </>
        )}
        <p className="auth-link">
          Don't have an account? <Link to="/register">Register</Link>
        </p>
      </div>
    </div>
  )
}
