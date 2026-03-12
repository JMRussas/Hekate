// Orchestration Engine - Register Page
//
// Registration form with link to login.
//
// Depends on: api/auth.ts, hooks/useAuth.tsx
// Used by:    App.tsx

import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { apiRegister, fetchOIDCProviders, type OIDCProvider } from '../api/auth'
import { useAuth } from '../hooks/useAuth'

export default function Register() {
  const { login, loginWithOIDC } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [providers, setProviders] = useState<OIDCProvider[]>([])

  useEffect(() => { fetchOIDCProviders().then(setProviders) }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await apiRegister(email, password, displayName)
      await login(email, password)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h1>Orchestration Engine</h1>
        <h2>Create Account</h2>
        {error && <div className="auth-error">{error}</div>}
        <form onSubmit={handleSubmit}>
          <label htmlFor="register-name">
            Display Name
            <input
              id="register-name"
              type="text"
              value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder="Optional"
            />
          </label>
          <label htmlFor="register-email">
            Email
            <input
              id="register-email"
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              required
            />
          </label>
          <label htmlFor="register-password">
            Password
            <input
              id="register-password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              minLength={8}
            />
          </label>
          <button type="submit" disabled={loading}>
            {loading ? 'Creating account...' : 'Create Account'}
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
          Already have an account? <Link to="/login">Sign In</Link>
        </p>
      </div>
    </div>
  )
}
