import { useEffect, useState } from 'react'
import { useAuth } from '../lib/AuthContext'
import { adminStats, adminListUsers, adminDeleteUser, adminListAnalyses } from '../lib/resources'
import { scoreColor, formatScore, STATUS_LABEL } from '../lib/format'
import { ApiError } from '../lib/api'

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'users', label: 'Users' },
  { key: 'analyses', label: 'Analyses' },
]

export default function Admin() {
  const [tab, setTab] = useState('overview')

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '40px 40px 100px' }}>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>Admin</div>
      <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
        Global users and analyses across every account.
      </div>

      <div style={{ display: 'flex', gap: 6, marginTop: 28, borderBottom: '1px solid var(--color-border)' }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              border: 'none',
              background: 'none',
              padding: '12px 16px',
              fontSize: 14,
              cursor: 'pointer',
              color: tab === t.key ? '#171717' : '#8c8c85',
              borderBottom: `2px solid ${tab === t.key ? '#171717' : 'transparent'}`,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 24 }}>
        {tab === 'overview' && <OverviewTab />}
        {tab === 'users' && <UsersTab />}
        {tab === 'analyses' && <AnalysesTab />}
      </div>
    </div>
  )
}

function OverviewTab() {
  const [stats, setStats] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    adminStats()
      .then(setStats)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load stats.'))
  }, [])

  if (error) return <div className="msg-error">{error}</div>
  if (!stats) return <div style={{ fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>

  return (
    <div>
      <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 12 }}>Users</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5,1fr)', gap: 16 }}>
        <StatCard label="Total" value={stats.users.total} />
        <StatCard label="Active" value={stats.users.active} />
        <StatCard label="Staff" value={stats.users.staff} />
        <StatCard label="Verified" value={stats.users.verified} />
        <StatCard label="Joined (7d)" value={stats.users.joined_last_7_days} />
      </div>

      <div style={{ fontWeight: 600, fontSize: 15, margin: '28px 0 12px' }}>Analyses</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5,1fr)', gap: 16 }}>
        <StatCard label="Total" value={stats.analyses.total} />
        <StatCard label="Completed" value={stats.analyses.completed} />
        <StatCard label="Failed" value={stats.analyses.failed} />
        <StatCard label="Total LOC" value={stats.analyses.total_lines_of_code} />
        <StatCard
          label="Avg. quality"
          value={stats.analyses.average_quality_score != null ? `${Math.round(stats.analyses.average_quality_score)}%` : '—'}
        />
      </div>
    </div>
  )
}

function StatCard({ label, value }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 24, marginTop: 4 }}>{value}</div>
    </div>
  )
}

function UsersTab() {
  const { user: currentUser } = useAuth()
  const [users, setUsers] = useState(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState(null)

  const load = (q = query) => {
    adminListUsers(q)
      .then((data) => setUsers(data.results))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load users.'))
  }

  useEffect(() => {
    const handle = setTimeout(() => load(query), 300)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const handleDelete = async (u) => {
    if (!window.confirm(`Delete user "${u.username}"? This cannot be undone.`)) return
    setBusyId(u.id)
    try {
      await adminDeleteUser(u.id)
      setUsers((prev) => prev.filter((x) => x.id !== u.id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not delete user.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div>
      <input
        type="text"
        placeholder="Search by username or email…"
        className="field"
        style={{ width: 320 }}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {error && <div className="msg-error" style={{ marginTop: 16 }}>{error}</div>}

      <div className="card" style={{ marginTop: 16, overflow: 'hidden' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 0.6fr 0.6fr 0.6fr 0.8fr', padding: '12px 16px', background: '#fafaf8', fontSize: 12, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)' }}>
          <span>USER</span>
          <span>EMAIL</span>
          <span>ANALYSES</span>
          <span>STAFF</span>
          <span>VERIFIED</span>
          <span></span>
        </div>
        {users === null && <div style={{ padding: 16, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
        {users?.map((u) => (
          <div key={u.id} style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 0.6fr 0.6fr 0.6fr 0.8fr', padding: '12px 16px', borderTop: '1px solid #f2f2ef', fontSize: 13, alignItems: 'center' }}>
            <span>{u.username}</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{u.email}</span>
            <span>{u.analyses_count}</span>
            <span>{u.is_staff ? 'Yes' : '—'}</span>
            <span>{u.is_verified ? 'Yes' : '—'}</span>
            <span style={{ textAlign: 'right' }}>
              {u.id !== currentUser?.id && (
                <button
                  className="btn btn-outline"
                  style={{ padding: '6px 12px', fontSize: 12 }}
                  disabled={busyId === u.id}
                  onClick={() => handleDelete(u)}
                >
                  Delete
                </button>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function AnalysesTab() {
  const [rows, setRows] = useState(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    adminListAnalyses({ status: statusFilter || undefined })
      .then((data) => setRows(data.results))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load analyses.'))
  }, [statusFilter])

  return (
    <div>
      <select className="field" style={{ width: 200 }} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
        <option value="">All statuses</option>
        {Object.entries(STATUS_LABEL).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>

      {error && <div className="msg-error" style={{ marginTop: 16 }}>{error}</div>}

      <div className="card" style={{ marginTop: 16, overflow: 'hidden' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 0.8fr 0.6fr 0.8fr', padding: '12px 16px', background: '#fafaf8', fontSize: 12, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)' }}>
          <span>NAME</span>
          <span>OWNER</span>
          <span>LANGUAGE</span>
          <span>SCORE</span>
          <span>STATUS</span>
        </div>
        {rows === null && <div style={{ padding: 16, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
        {rows?.map((r) => (
          <div key={r.id} style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 0.8fr 0.6fr 0.8fr', padding: '12px 16px', borderTop: '1px solid #f2f2ef', fontSize: 13, alignItems: 'center' }}>
            <span>{r.name}</span>
            <span style={{ color: 'var(--color-text-secondary)' }}>{r.owner_username}</span>
            <span>{r.language}</span>
            <span style={{ fontWeight: 600, color: scoreColor(r.quality_score) }}>{formatScore(r.quality_score)}</span>
            <span>{STATUS_LABEL[r.status] || r.status}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
