const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

// Auth is entirely httpOnly cookies now (access_token/refresh_token, set by
// the backend on login/refresh - see accounts/cookies.py) - frontend JS never
// sees the raw tokens, so there's nothing to store or attach as a header here.

export class ApiError extends Error {
  constructor(status, data) {
    super(extractMessage(data) || `Request failed (${status})`)
    this.status = status
    this.data = data
  }
}

function extractMessage(data) {
  if (!data) return null
  const firstString = (val) => {
    const text = Array.isArray(val) ? val[0] : val
    return typeof text === 'string' ? text : null
  }
  // `detail` and `non_field_errors` describe the request as a whole rather
  // than one field of it, so they're shown verbatim. Running them through the
  // prefixing branch below put DRF's internal field naming in front of users -
  // a failed login read "non_field_errors: No active account found...".
  for (const key of ['detail', 'non_field_errors']) {
    const text = firstString(data[key])
    if (text) return text
  }
  // A genuine per-field error keeps its key, which is what makes it findable
  // on a form with several inputs.
  const firstKey = Object.keys(data)[0]
  if (firstKey) {
    const text = firstString(data[firstKey])
    if (text) return `${firstKey}: ${text}`
  }
  return null
}

// status 0 signals "the request never got a response at all" (offline, DNS
// failure, CORS rejection, backend down) - distinct from any real HTTP status
// the server returned, so callers checking `err.status` can tell them apart.
const NETWORK_ERROR_DATA = { detail: 'Unable to reach the server. Check your connection and try again.' }

// fetch() only rejects for network-level failures, never for HTTP error
// statuses - wrapping it here means every caller of doFetch()/refreshAccessToken()
// only ever has to deal with ApiError, not a raw TypeError from the browser.
async function safeFetch(...args) {
  try {
    return await fetch(...args)
  } catch {
    throw new ApiError(0, NETWORK_ERROR_DATA)
  }
}

async function safeJson(res) {
  try {
    return await res.json()
  } catch {
    return null
  }
}

const CSRF_COOKIE_RE = /(?:^|;\s*)csrftoken=([^;]+)/

// Read fresh from document.cookie rather than caching - primeCsrf() can
// re-issue it and this is cheap. Only meaningful for cookie-authenticated
// requests; Django's CSRF check itself only applies to unsafe methods.
function csrfHeaders() {
  if (typeof document === 'undefined') return {}
  const match = document.cookie.match(CSRF_COOKIE_RE)
  return match ? { 'X-CSRFToken': decodeURIComponent(match[1]) } : {}
}

// Primes the (JS-readable, non-httpOnly) csrftoken cookie so every mutating
// request afterward has something to echo back. Call once on app boot -
// before that, any POST/PUT/PATCH/DELETE would 403 with "CSRF token missing".
export async function primeCsrf() {
  await safeFetch(`${API_BASE_URL}/auth/csrf/`, { credentials: 'include' })
}

let refreshPromise = null

async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise

  refreshPromise = (async () => {
    const res = await safeFetch(`${API_BASE_URL}/auth/refresh/`, {
      method: 'POST',
      credentials: 'include',
      headers: csrfHeaders(),
    })
    if (!res.ok) throw new ApiError(res.status, await safeJson(res))
  })()

  try {
    await refreshPromise
  } finally {
    refreshPromise = null
  }
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, isForm?: boolean, auth?: boolean, responseType?: 'json'|'blob'}} opts
 */
export async function apiFetch(path, opts = {}) {
  const { method = 'GET', body, isForm = false, auth = true, responseType = 'json' } = opts

  const doFetch = () => {
    const headers = { ...csrfHeaders() }
    if (!isForm) headers['Content-Type'] = 'application/json'

    return safeFetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      credentials: 'include',
      body: body == null ? undefined : isForm ? body : JSON.stringify(body),
    })
  }

  let res = await doFetch()

  if (res.status === 401 && auth) {
    try {
      await refreshAccessToken()
      res = await doFetch()
    } catch (err) {
      if (err instanceof ApiError && err.status === 0) throw err
      throw new ApiError(401, { detail: 'Session expired. Please sign in again.' })
    }
  }

  if (!res.ok) {
    throw new ApiError(res.status, await safeJson(res))
  }

  if (responseType === 'blob') return res.blob()
  if (res.status === 204) return null
  return safeJson(res)
}

/**
 * Single reporting point for client-side failures that would otherwise be
 * swallowed - a caught-but-unexpected error, or a React render crash from
 * ErrorBoundary.
 *
 * Today this writes to the console, which is all the project has: there is no
 * error-tracking service configured (see docs/OPERATIONS.md). The value of
 * routing through one function anyway is that wiring a real service later is a
 * change here rather than a hunt through every catch block, and that the
 * places which deliberately report are now greppable.
 */
export function reportClientError(message, error, context = {}) {
  const detail = { message, context }
  if (error instanceof ApiError) {
    detail.status = error.status
    detail.data = error.data
  } else if (error) {
    detail.error = error
  }
  // eslint-disable-next-line no-console
  console.error('[client-error]', detail)
}

export { API_BASE_URL }
