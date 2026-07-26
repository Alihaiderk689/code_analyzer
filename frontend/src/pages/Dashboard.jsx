import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { getDashboardStats, getDashboardRecent, getDashboardLanguages, getDashboardScores } from '../lib/resources'
import { scoreColor, formatScore, formatDate } from '../lib/format'
import { ApiError } from '../lib/api'

const DISTRIBUTION_BUCKETS = [
  { key: 'excellent', label: 'Excellent (≥90)', color: 'var(--color-success)' },
  { key: 'good', label: 'Good (≥70)', color: 'var(--color-accent)' },
  { key: 'fair', label: 'Fair (≥50)', color: 'var(--color-warning)' },
  { key: 'poor', label: 'Poor (<50)', color: 'var(--color-danger)' },
]

export default function Dashboard() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [stats, setStats] = useState(null)
  const [recent, setRecent] = useState(null)
  const [languages, setLanguages] = useState(null)
  const [scores, setScores] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([getDashboardStats(), getDashboardRecent(5), getDashboardLanguages(), getDashboardScores()])
      .then(([statsRes, recentRes, languagesRes, scoresRes]) => {
        setStats(statsRes)
        setRecent(recentRes.results)
        setLanguages(languagesRes.languages)
        setScores(scoresRes)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load dashboard.'))
  }, [])

  const displayName = user?.first_name || user?.username || ''

  if (error) {
    return (
      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '40px 40px 80px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (!stats) {
    return <div className="page-loading">Loading dashboard…</div>
  }

  const maxLangCount = Math.max(...languages.map((l) => l.analyses_count), 1)
  const maxBucketCount = Math.max(...Object.values(scores.distribution), 1)

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '40px 40px 80px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>
            Welcome back{displayName ? `, ${displayName}` : ''}
          </div>
          <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
            Here's what's happening with your code.
          </div>
        </div>
        <button className="btn btn-dark" style={{ padding: '13px 22px', fontSize: 14 }} onClick={() => navigate('/analyze')}>
          + New Analysis
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 20, marginTop: 32 }}>
        <StatCard label="Total analyses" value={stats.total_analyses} />
        <StatCard label="Completed analyses" value={stats.completed} />
        <StatCard
          label="Avg. quality score"
          value={stats.average_quality_score != null ? `${Math.round(stats.average_quality_score)}%` : '—'}
        />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.3fr', gap: 20, marginTop: 20 }}>
        <div className="card" style={{ padding: 24 }}>
          <div style={{ fontWeight: 600, fontSize: 15 }}>Languages used</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 20 }}>
            {languages.length === 0 && <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>No analyses yet.</div>}
            {languages.map((lang) => (
              <div key={lang.language}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 6 }}>
                  <span style={{ fontFamily: 'var(--font-mono)' }}>{lang.language}</span>
                  <span style={{ color: 'var(--color-text-secondary-2)' }}>{lang.analyses_count}</span>
                </div>
                <div style={{ height: 8, background: 'var(--color-bg-subtle)', borderRadius: 100, overflow: 'hidden' }}>
                  <div
                    style={{
                      height: '100%',
                      background: 'var(--color-accent)',
                      borderRadius: 100,
                      width: `${(lang.analyses_count / maxLangCount) * 100}%`,
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="card" style={{ padding: 24 }}>
          <div style={{ fontWeight: 600, fontSize: 15 }}>Recent analyses</div>
          <div style={{ display: 'flex', flexDirection: 'column', marginTop: 16 }}>
            {recent.length === 0 && (
              <div style={{ fontSize: 13, color: 'var(--color-text-muted)', padding: '12px 0' }}>
                No analyses yet — run your first one.
              </div>
            )}
            {recent.map((row) => (
              <div
                key={row.id}
                onClick={() => navigate(`/report/${row.id}`)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '12px 0',
                  borderBottom: '1px solid var(--color-border)',
                  cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: 13, background: 'var(--color-bg-subtle)', padding: '4px 8px', borderRadius: 6 }}>
                    {row.language}
                  </div>
                  <span style={{ fontSize: 14 }}>{row.name}</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                  <span style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>{formatDate(row.created_at)}</span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: scoreColor(row.quality_score) }}>
                    {formatScore(row.quality_score)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: 24, marginTop: 20 }}>
        <div style={{ fontWeight: 600, fontSize: 15 }}>Score distribution</div>
        {scores.scored_count === 0 ? (
          <div style={{ fontSize: 13, color: 'var(--color-text-muted)', marginTop: 12 }}>No scored analyses yet.</div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16, marginTop: 20 }}>
            {DISTRIBUTION_BUCKETS.map((bucket) => {
              const count = scores.distribution[bucket.key] || 0
              return (
                <div key={bucket.key}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 6 }}>
                    <span>{bucket.label}</span>
                    <span style={{ color: 'var(--color-text-secondary-2)' }}>{count}</span>
                  </div>
                  <div style={{ height: 8, background: 'var(--color-bg-subtle)', borderRadius: 100, overflow: 'hidden' }}>
                    <div
                      style={{
                        height: '100%',
                        background: bucket.color,
                        borderRadius: 100,
                        width: `${(count / maxBucketCount) * 100}%`,
                      }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

function StatCard({ label, value }) {
  return (
    <div className="card" style={{ padding: 22 }}>
      <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 38, marginTop: 6 }}>{value}</div>
    </div>
  )
}
