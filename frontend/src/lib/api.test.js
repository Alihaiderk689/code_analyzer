import { describe, expect, it } from 'vitest'
import { ApiError } from './api'

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
