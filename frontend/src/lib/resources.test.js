import { afterEach, describe, expect, it, vi } from 'vitest'
import { getDashboard, getRefactor } from './resources'

// The dashboard page used to open with four parallel requests
// (/dashboard/stats/, /recent/, /languages/, /scores/). The backend runs three
// synchronous gunicorn workers, so that was one page load demanding four of
// them at once. Dashboard.jsx now calls getDashboard() alone.
//
// vitest.config.js runs these in a plain Node environment with no DOM, so the
// page component itself cannot be rendered here - what this pins is the part
// that carries the regression risk: that the collapsed call is one request, to
// the summary endpoint.
describe('getDashboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function stubFetch(payload) {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('issues exactly one request, to the summary endpoint', async () => {
    const fetchMock = stubFetch({ stats: {}, recent_analyses: [], top_languages: [], languages: [], scores: {} })

    await getDashboard()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/dashboard\/$/)
    expect(options.method).toBe('GET')
    // Cookie auth: the tokens are httpOnly, so the request is only
    // authenticated if credentials ride along.
    expect(options.credentials).toBe('include')
  })

  it('returns the summary body unwrapped, with the keys Dashboard.jsx reads', async () => {
    stubFetch({
      stats: { total_analyses: 3, completed: 2, average_quality_score: 70 },
      recent_analyses: [{ id: 1, name: 'a.py' }],
      top_languages: [{ language: 'Python', analyses_count: 2 }],
      languages: [{ language: 'Python', analyses_count: 2 }, { language: 'Go', analyses_count: 1 }],
      scores: { scored_count: 2, distribution: { excellent: 1, good: 1, fair: 0, poor: 0 } },
    })

    const data = await getDashboard()

    expect(data.stats.total_analyses).toBe(3)
    expect(data.recent_analyses).toHaveLength(1)
    expect(data.scores.distribution.excellent).toBe(1)
    // The page renders every language, so it reads `languages` - reading
    // `top_languages` here would silently cap the card at five.
    expect(data.languages).toHaveLength(2)
  })
})

// "Try Another Refactor" in the Refactored Code tab is the only thing that
// gets past RefactorView's cache: without ?regenerate=true the backend returns
// the saved result and never calls the AI (analyses/ai_views.py RefactorView).
// The flag is compared literally against the string "true", so the exact
// spelling of this query string is load-bearing.
describe('getRefactor', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function stubFetch() {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ refactored_code: '', explanation: [], cached: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('reads the saved refactor by default', async () => {
    const fetchMock = stubFetch()
    await getRefactor(7)
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/analysis\/7\/refactor\/$/)
  })

  it('asks for a fresh one when regenerate is requested', async () => {
    const fetchMock = stubFetch()
    await getRefactor(7, true)
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/analysis\/7\/refactor\/\?regenerate=true$/)
  })
})
