import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listHistory, searchAnalyses, deleteAnalysis, clearHistory } from '../lib/resources'
import { scoreColor, formatScore, STATUS_LABEL } from '../lib/format'
import { ApiError } from '../lib/api'

const GRID_COLUMNS = '1.3fr 0.9fr 0.7fr 0.9fr 0.6fr'
const PAGE_SIZE = 20

export default function History() {
  const navigate = useNavigate()
  const [rows, setRows] = useState(null)
  const [count, setCount] = useState(0)
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState(null)
  const [clearing, setClearing] = useState(false)

  const applyPage = (data) => {
    setRows(data.results)
    setCount(data.count)
  }

  useEffect(() => {
    listHistory({ page: 1 })
      .then(applyPage)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load history.'))
  }, [])

  // Resets to page 1 whenever the search query changes (debounced), so
  // switching from "browsing page 3 of everything" to a new search term
  // doesn't silently request a page number that may not exist for the new
  // result set.
  useEffect(() => {
    const trimmed = query.trim()
    const handle = setTimeout(() => {
      setPage(1)
      const load = trimmed ? searchAnalyses(trimmed, { page: 1 }) : listHistory({ page: 1 })
      load.then(applyPage).catch((err) => setError(err instanceof ApiError ? err.message : 'Search failed.'))
    }, 300)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  useEffect(() => {
    if (page === 1) return // already loaded by the effects above
    const trimmed = query.trim()
    const load = trimmed ? searchAnalyses(trimmed, { page }) : listHistory({ page })
    load.then(applyPage).catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load history.'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page])

  const handleDelete = async (e, row) => {
    e.stopPropagation()
    if (!window.confirm(`Delete "${row.name}"? This cannot be undone.`)) return
    setBusyId(row.id)
    try {
      await deleteAnalysis(row.id)
      setRows((prev) => prev.filter((r) => r.id !== row.id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not delete analysis.')
    } finally {
      setBusyId(null)
    }
  }

  const handleClearAll = async () => {
    if (!count) return
    if (!window.confirm(`Delete all ${count} analyses? This cannot be undone.`)) return
    setClearing(true)
    try {
      await clearHistory()
      setRows([])
      setCount(0)
      setPage(1)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not clear history.')
    } finally {
      setClearing(false)
    }
  }

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: '44px 40px 100px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>Analysis history</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <input
            type="text"
            placeholder="Search by name, language, or code…"
            className="field"
            style={{ width: 280 }}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button
            className="btn btn-outline"
            style={{ padding: '9px 16px', fontSize: 13, whiteSpace: 'nowrap' }}
            disabled={clearing || !count}
            onClick={handleClearAll}
          >
            {clearing ? 'Clearing…' : 'Clear all'}
          </button>
        </div>
      </div>

      {error && <div className="msg-error" style={{ marginTop: 20 }}>{error}</div>}

      <div className="card" style={{ marginTop: 24, overflow: 'hidden' }}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: GRID_COLUMNS,
            padding: '14px 20px',
            background: 'var(--color-bg-subtle)',
            fontSize: 12,
            color: 'var(--color-text-secondary-2)',
            fontFamily: 'var(--font-mono)',
          }}
        >
          <span>FILE</span>
          <span>LANGUAGE</span>
          <span>SCORE</span>
          <span>STATUS</span>
          <span></span>
        </div>

        {rows === null && <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
        {rows?.length === 0 && (
          <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>No analyses found.</div>
        )}
        {rows?.map((row) => (
          <div
            key={row.id}
            onClick={() => navigate(`/report/${row.id}`)}
            style={{
              display: 'grid',
              gridTemplateColumns: GRID_COLUMNS,
              padding: '16px 20px',
              borderTop: '1px solid var(--color-border)',
              fontSize: 14,
              alignItems: 'center',
              cursor: 'pointer',
            }}
          >
            <span>{row.name}</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{row.language}</span>
            <span style={{ fontWeight: 600, color: scoreColor(row.quality_score) }}>{formatScore(row.quality_score)}</span>
            <span style={{ color: row.status === 'failed' ? 'var(--color-danger)' : 'var(--color-success)' }}>
              {STATUS_LABEL[row.status] || row.status}
            </span>
            <span style={{ textAlign: 'right' }}>
              <button
                className="btn btn-outline"
                style={{ padding: '6px 12px', fontSize: 12 }}
                disabled={busyId === row.id}
                onClick={(e) => handleDelete(e, row)}
              >
                Delete
              </button>
            </span>
          </div>
        ))}
      </div>

      {count > PAGE_SIZE && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 16, marginTop: 20 }}>
          <button
            className="btn btn-outline"
            style={{ padding: '8px 16px', fontSize: 13 }}
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            ← Previous
          </button>
          <span style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>
            Page {page} of {Math.max(1, Math.ceil(count / PAGE_SIZE))}
          </span>
          <button
            className="btn btn-outline"
            style={{ padding: '8px 16px', fontSize: 13 }}
            disabled={page >= Math.max(1, Math.ceil(count / PAGE_SIZE))}
            onClick={() => setPage((p) => p + 1)}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
