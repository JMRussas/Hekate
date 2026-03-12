// Orchestration Engine - Auth Hook & Context
//
// Provides authentication state across the app.
// Auto-refreshes tokens before they expire.
//
// Depends on: api/auth.ts
// Used by:    App.tsx, components/AuthGuard.tsx, Layout.tsx

import { createContext, useContext, useEffect, useState, useCallback, useRef } from 'react'
import type { ReactNode } from 'react'
import {
  type User,
  apiGetMe,
  apiLogin,
  apiRefresh,
  clearTokens,
  getAccessToken,
  setTokens,
  startOIDCLogin,
} from '../api/auth'

interface AuthState {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  loginWithOIDC: (provider: string) => Promise<void>
  setUserFromOIDC: (user: User) => void
  logout: () => void
}

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  login: async () => {},
  loginWithOIDC: async () => {},
  setUserFromOIDC: () => {},
  logout: () => {},
})

export function useAuth(): AuthState {
  return useContext(AuthContext)
}

// Token refresh interval (25 minutes — before the default 30-min expiry)
const REFRESH_INTERVAL_MS = 25 * 60 * 1000

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  const startRefreshTimer = useCallback(() => {
    if (refreshTimer.current) clearInterval(refreshTimer.current)
    refreshTimer.current = setInterval(async () => {
      const ok = await apiRefresh()
      if (!ok) {
        setUser(null)
      }
    }, REFRESH_INTERVAL_MS)
  }, [])

  const stopRefreshTimer = useCallback(() => {
    if (refreshTimer.current) {
      clearInterval(refreshTimer.current)
      refreshTimer.current = null
    }
  }, [])

  // Check for existing token on mount
  useEffect(() => {
    const token = getAccessToken()
    if (!token) {
      setLoading(false)
      return
    }
    apiGetMe()
      .then(u => {
        setUser(u)
        startRefreshTimer()
      })
      .catch(async () => {
        // Try refreshing
        const ok = await apiRefresh()
        if (ok) {
          try {
            const u = await apiGetMe()
            setUser(u)
            startRefreshTimer()
          } catch {
            clearTokens()
          }
        } else {
          clearTokens()
        }
      })
      .finally(() => setLoading(false))

    return () => stopRefreshTimer()
  }, [startRefreshTimer, stopRefreshTimer])

  const login = useCallback(async (email: string, password: string) => {
    const resp = await apiLogin(email, password)
    setUser(resp.user)
    startRefreshTimer()
  }, [startRefreshTimer])

  const loginWithOIDC = useCallback(async (provider: string) => {
    await startOIDCLogin(provider)
  }, [])

  const setUserFromOIDC = useCallback((u: User) => {
    setUser(u)
    startRefreshTimer()
  }, [startRefreshTimer])

  const logout = useCallback(() => {
    clearTokens()
    setUser(null)
    stopRefreshTimer()
  }, [stopRefreshTimer])

  return (
    <AuthContext.Provider value={{ user, loading, login, loginWithOIDC, setUserFromOIDC, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
