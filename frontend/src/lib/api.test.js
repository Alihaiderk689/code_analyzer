import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, ApiError, primeCsrf } from './api'

// ApiError's message-extraction logic decides what every page in the app
// shows the user when a request fails, so it's worth pinning down directly.
describe('ApiError', () => {
  it('prefers a top-level "detail" string', () => {
    const err = new ApiError(400, { detail: 'Invalid or expired reset link.' })
    expect(err.message).toBe('Invalid or expired reset link.')
    expect(err.status).toBe(400)
  })

  it('falls back to the first field error, unwrapping arrays', () => {
    const err = new ApiError(400, { new_password2: ['Passwords do not match.'] })
    expect(err.message).toBe('new_password2: Passwords do not match.')
  })

  it('falls back to a generic message when there is no parsable body', () => {
    expect(new ApiError(500, null).message).toBe('Request failed (500)')
    expect(new ApiError(503, {}).message).toBe('Request failed (503)')
  })

  it('ignores non-string field values it cannot render', () => {
    const err = new ApiError(400, { count: 3 })
    expect(err.message).toBe('Request failed (400)')
  })
})

// A network-level failure (offline, DNS, CORS, backend down) makes fetch()
// reject with a raw TypeError rather than resolving with a bad status - every
// caller in the app expects an ApiError, so apiFetch must convert it.
describe('apiFetch network failures', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('wraps a rejected fetch() in an ApiError with status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(apiFetch('/health/', { auth: false })).rejects.toMatchObject({
      status: 0,
      message: 'Unable to reach the server. Check your connection and try again.',
    })
  })
})

// primeCsrf() must read the token from the /auth/csrf/ response body, not
// rely on document.cookie - that cookie is only readable by frontend JS when
// frontend and backend share a registrable domain, which isn't true of every
// deployment topology (e.g. a frontend and backend on unrelated hosts). This
// test environment has no `document` at all (see vitest.config.js), which
// doubles as proof the in-memory token path doesn't depend on one.
describe('primeCsrf / csrfHeaders', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('caches the csrfToken from the response body and echoes it as X-CSRFToken', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ csrfToken: 'body-token-123' }) })
      .mockResolvedValueOnce({ ok: true, status: 204 })
    vi.stubGlobal('fetch', fetchMock)

    await primeCsrf()
    await apiFetch('/some/endpoint/', { method: 'POST', body: { a: 1 } })

    const [, requestInit] = fetchMock.mock.calls[1]
    expect(requestInit.headers['X-CSRFToken']).toBe('body-token-123')
  })
})
