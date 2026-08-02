import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { primeCsrf, ApiError } from './api'
import { loginUser, logoutUser, fetchProfile, updateProfile, adminStats } from './resources'

// The profile endpoint never returns is_staff, so the only reliable way to know
// whether the current user is an admin is to probe an admin-only endpoint and see
// whether it 403s. Cheap enough to run once per session/login.
async function checkIsAdmin() {
  try {
    await adminStats()
    return true
  } catch (err) {
    if (err instanceof ApiError && (err.status === 403 || err.status === 401)) return false
    return false
  }
}

const PENDING_FIRST_NAME_KEY = 'ca_pending_first_name'
const PENDING_LAST_NAME_KEY = 'ca_pending_last_name'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [initializing, setInitializing] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function restore() {
      // The CSRF cookie must exist before any mutating request this session
      // might make; access/refresh tokens are httpOnly, so JS can't check
      // for them up front the way it used to - just attempt the profile
      // fetch and let a 401 (even after apiFetch's automatic refresh retry)
      // mean "not logged in".
      await primeCsrf()
      try {
        const profile = await fetchProfile()
        const admin = await checkIsAdmin()
        if (!cancelled) {
          setUser(profile)
          setIsAdmin(admin)
        }
      } catch {
        // Not logged in (or session expired) - nothing to clean up client-side
        // anymore, the cookies are what they are.
      } finally {
        if (!cancelled) setInitializing(false)
      }
    }
    restore()
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email, password) => {
    const data = await loginUser(email, password)

    const pendingFirstName = sessionStorage.getItem(PENDING_FIRST_NAME_KEY)
    const pendingLastName = sessionStorage.getItem(PENDING_LAST_NAME_KEY)
    let finalUser = data.user
    if (pendingFirstName || pendingLastName) {
      sessionStorage.removeItem(PENDING_FIRST_NAME_KEY)
      sessionStorage.removeItem(PENDING_LAST_NAME_KEY)
      try {
        const profile = await updateProfile({
          first_name: pendingFirstName || '',
          last_name: pendingLastName || '',
        })
        finalUser = { ...data.user, ...profile }
      } catch {
        // Name is a nice-to-have; don't block login if this fails.
      }
    }

    setUser(finalUser)
    const admin = await checkIsAdmin()
    setIsAdmin(admin)
    return { user: finalUser, isAdmin: admin }
  }, [])

  const logout = useCallback(async () => {
    try {
      await logoutUser()
    } catch {
      // best-effort; clear local UI state regardless
    }
    setUser(null)
    setIsAdmin(false)
  }, [])

  const refreshUser = useCallback(async () => {
    const profile = await fetchProfile()
    setUser(profile)
    return profile
  }, [])

  return (
    <AuthContext.Provider value={{ user, isAdmin, initializing, login, logout, refreshUser, setUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
