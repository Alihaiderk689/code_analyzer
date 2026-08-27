import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { primeCsrf, ApiError, reportClientError } from './api'
import { loginUser, logoutUser, fetchProfile, updateProfile, adminStats, googleLogin } from './resources'

// The profile endpoint never returns is_staff, so the only reliable way to know
// whether the current user is an admin is to probe an admin-only endpoint and see
// whether it 403s. Cheap enough to run once per session/login.
//
// Returns { isAdmin, indeterminate }. Both failure modes deny admin - a
// permission check must fail closed - but they are no longer the same event:
// a 401/403 is a real answer ("you are not an admin"), while a network failure
// or a 5xx means we never found out. Collapsing the two silently downgraded a
// genuine admin for their whole session on one transient blip, with nothing
// logged and no way to tell it had happened.
async function checkIsAdmin() {
  try {
    await adminStats()
    return { isAdmin: true, indeterminate: false }
  } catch (err) {
    if (err instanceof ApiError && (err.status === 403 || err.status === 401)) {
      return { isAdmin: false, indeterminate: false }
    }
    reportClientError('checkIsAdmin failed; treating as non-admin for this session', err)
    return { isAdmin: false, indeterminate: true }
  }
}

const PENDING_FIRST_NAME_KEY = 'ca_pending_first_name'
const PENDING_LAST_NAME_KEY = 'ca_pending_last_name'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [initializing, setInitializing] = useState(true)
  // True when session restore failed because the server was unreachable, as
  // opposed to the user simply not being logged in.
  const [offline, setOffline] = useState(false)
  // True when the admin probe could not complete (network/5xx). Admin access
  // is denied either way; this exists so the state is diagnosable rather than
  // looking like a deliberate permission decision.
  const [adminCheckFailed, setAdminCheckFailed] = useState(false)

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
          setIsAdmin(admin.isAdmin)
          setAdminCheckFailed(admin.indeterminate)
        }
      } catch (err) {
        // status 0 means the request never reached the server (offline, DNS,
        // CORS, backend down) - that is NOT the same as "not logged in", and
        // showing the logged-out UI to someone holding perfectly valid cookies
        // sends them to a login form that cannot work either. Record it so the
        // app can say "can't reach the server" instead.
        if (!cancelled && err instanceof ApiError && err.status === 0) {
          setOffline(true)
          reportClientError('Session restore failed: server unreachable', err)
        }
        // Any other error (401 after the automatic refresh retry, 403) really
        // does mean not logged in - nothing to clean up, the cookies are what
        // they are.
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
    // checkIsAdmin() returns { isAdmin, indeterminate } - unwrap it. Storing
    // the object made isAdmin truthy for EVERY account that signed in through
    // this path, so ordinary users were routed to /admin (and UserRoute then
    // bounced them off /dashboard), landing on a page where every call 403s.
    // The session-restore path above already unwraps it, which is why a
    // page reload behaved correctly and only a fresh login did not.
    setIsAdmin(admin.isAdmin)
    setAdminCheckFailed(admin.indeterminate)
    return { user: finalUser, isAdmin: admin.isAdmin }
  }, [])

  // Google already gives us a verified email + name up front, so there's no
  // pending-name staging to apply here the way email/password login does.
  const loginWithGoogle = useCallback(async (accessToken) => {
    const data = await googleLogin(accessToken)
    setUser(data.user)
    const admin = await checkIsAdmin()
    // Same unwrapping as login() above - see the comment there.
    setIsAdmin(admin.isAdmin)
    setAdminCheckFailed(admin.indeterminate)
    return { user: data.user, isAdmin: admin.isAdmin }
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
    <AuthContext.Provider
      value={{ user, isAdmin, initializing, offline, adminCheckFailed, login, loginWithGoogle, logout, refreshUser, setUser }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
