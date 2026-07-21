const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

const ACCESS_KEY = 'ca_access'
const REFRESH_KEY = 'ca_refresh'

export function getAccessToken() {
  return localStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens({ access, refresh }) {
  if (access) localStorage.setItem(ACCESS_KEY, access)
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh)
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export class ApiError extends Error {
  constructor(status, data) {
    super(extractMessage(data) || `Request failed (${status})`)
    this.status = status
    this.data = data
  }
}

function extractMessage(data) {
  if (!data) return null
  if (typeof data.detail === 'string') return data.detail
  const firstKey = Object.keys(data)[0]
  if (firstKey) {
    const val = data[firstKey]
    const text = Array.isArray(val) ? val[0] : val
    if (typeof text === 'string') return `${firstKey}: ${text}`
  }
  return null
}

let refreshPromise = null

async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise

  refreshPromise = (async () => {
    const refresh = getRefreshToken()
    if (!refresh) throw new ApiError(401, { detail: 'No refresh token.' })

    const res = await fetch(`${API_BASE_URL}/auth/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh }),
    })

    if (!res.ok) {
      clearTokens()
      throw new ApiError(res.status, await safeJson(res))
    }

    const data = await res.json()
    setTokens({ access: data.access, refresh: data.refresh })
    return data.access
  })()

  try {
    return await refreshPromise
  } finally {
    refreshPromise = null
  }
}

async function safeJson(res) {
  try {
    return await res.json()
  } catch {
    return null
  }
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, isForm?: boolean, auth?: boolean, responseType?: 'json'|'blob'}} opts
 */
export async function apiFetch(path, opts = {}) {
  const { method = 'GET', body, isForm = false, auth = true, responseType = 'json' } = opts

  const doFetch = async (accessToken) => {
    const headers = {}
    if (!isForm) headers['Content-Type'] = 'application/json'
    if (auth && accessToken) headers['Authorization'] = `Bearer ${accessToken}`

    return fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: body == null ? undefined : isForm ? body : JSON.stringify(body),
    })
  }

  let res = await doFetch(auth ? getAccessToken() : null)

  if (res.status === 401 && auth && getRefreshToken()) {
    try {
      const newAccess = await refreshAccessToken()
      res = await doFetch(newAccess)
    } catch {
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

export { API_BASE_URL }
