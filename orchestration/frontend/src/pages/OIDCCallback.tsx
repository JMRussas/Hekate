// Orchestration Engine - OIDC Callback Page
//
// Handles the redirect from the OIDC provider after authorization.
// Extracts code + state from URL params, exchanges for tokens.
//
// Depends on: api/auth.ts, hooks/useAuth.tsx
// Used by:    App.tsx

import { useEffect, useState, useRef } from 'react'
import { useNavigate, useSearchParams, Link } from 'react-router-dom'
import { completeOIDCLogin } from '../api/auth'
import { useAuth } from '../hooks/useAuth'

export default function OIDCCallback() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { setUserFromOIDC } = useAuth()
  const [error, setError] = useState('')
  const processedRef = useRef(false)

  useEffect(() => {
    if (processedRef.current) return
    processedRef.current = true

    const code = searchParams.get('code')
    const state = searchParams.get('state')
    const errorParam = searchParams.get('error')
    const provider = sessionStorage.getItem('oidc_provider')

    if (errorParam) {
      setError(`Authentication denied: ${searchParams.get('error_description') || errorParam}`)
      return
    }

    if (!code || !state || !provider) {
      setError('Invalid callback — missing code, state, or provider')
      return
    }

    completeOIDCLogin(provider, code, state)
      .then(resp => {
        setUserFromOIDC(resp.user)
        navigate('/')
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : 'OIDC authentication failed')
      })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (error) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <h1>Orchestration Engine</h1>
          <h2>Authentication Error</h2>
          <div className="auth-error">{error}</div>
          <p className="auth-link">
            <Link to="/login">Back to Sign In</Link>
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h2>Completing sign-in...</h2>
      </div>
    </div>
  )
}
