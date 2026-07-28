import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  getGitHubMetrics,
  getGitHubQualityTrends,
  listGitHubPullRequests,
  listMonitoredGitHubRepositories,
} from '../lib/resources'
import { formatScore, formatDate, scoreColor, STATUS_LABEL } from '../lib/format'
import { ApiError } from '../lib/api'

const TRENDS_WINDOW_DAYS = 30

const GRID_COLUMNS = '1.4fr 0.5fr 0.7fr 0.7fr'

export default function GitHubPullRequests() {
  const navigate = useNavigate()
  const [metrics, setMetrics] = useState(null)
  const [repository, setRepository] = useState(undefined)
  const [page, setPage] = useState(1)
  const [pullRequests, setPullRequests] = useState(null)
  const [trends, setTrends] = useState(null)
  const [error, setError] = useState('')

  // Hard-scoped to whichever single repo is actively monitored right now -
  // no cross-repo view, no picking a different (possibly stale) one. If you
  // switch which repo you monitor, this page switches with it.
  useEffect(() => {
    listMonitoredGitHubRepositories()
      .then((monitoredRes) => setRepository(monitoredRes[0] ?? null))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load your monitored repository.'))
  }, [])

  useEffect(() => {
    if (!repository) return
    const repositoryId = repository.id
    setMetrics(null)
    setPullRequests(null)
    setTrends(null)

    getGitHubMetrics({ repositoryId })
      .then(setMetrics)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load GitHub metrics.'))
    listGitHubPullRequests({ repositoryId, page })
      .then(setPullRequests)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load pull requests.'))
    getGitHubQualityTrends({ repositoryId, days: TRENDS_WINDOW_DAYS })
      .then((res) => setTrends(res.results))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load quality trends.'))
  }, [repository, page])

  if (error) {
    return (
      <div style={{ maxWidth: 1100, margin: '0 auto', padding: '44px 40px 100px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (repository === null) {
    return (
      <div style={{ maxWidth: 1100, margin: '0 auto', padding: '44px 40px 100px' }}>
        <PageHeader />
        <div className="card" style={{ padding: 24, marginTop: 24, fontSize: 14, color: 'var(--color-text-muted)' }}>
          You're not monitoring a repository right now. <Link to="/github">Connect one</Link> to see its pull request reviews here.
        </div>
      </div>
    )
  }

  if (repository === undefined || !metrics) {
    return <div className="page-loading">Loading…</div>
  }

  const pageSize = 20
  const totalPages = pullRequests ? Math.max(1, Math.ceil(pullRequests.count / pageSize)) : 1

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '44px 40px 100px' }}>
      <PageHeader repository={repository} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 20, marginTop: 32 }}>
        <StatCard label="PRs analyzed" value={metrics.total_prs_analyzed} />
        <StatCard label="Avg. quality score" value={metrics.average_quality_score != null ? `${Math.round(metrics.average_quality_score)}%` : '—'} />
        <StatCard label="Critical vulnerabilities" value={metrics.critical_vulnerabilities} />
        <StatCard label="Most common issue" value={metrics.most_common_issue_type ? metrics.most_common_issue_type.replace(/_/g, ' ') : '—'} />
      </div>

      <div className="card" style={{ padding: 24, marginTop: 20 }}>
        <div style={{ fontWeight: 600, fontSize: 15 }}>Quality trend (last {TRENDS_WINDOW_DAYS} days)</div>
        <TrendsChart trends={trends} />
      </div>

      <div className="card" style={{ marginTop: 24, overflow: 'hidden' }}>
        <div
          style={{
            display: 'grid', gridTemplateColumns: GRID_COLUMNS, padding: '14px 20px', background: 'var(--color-bg-subtle)',
            fontSize: 12, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)',
          }}
        >
          <span>PULL REQUEST</span>
          <span>#</span>
          <span>SCORE</span>
          <span>STATUS</span>
        </div>

        {pullRequests === null && <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
        {pullRequests?.results.length === 0 && (
          <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>
            No pull requests analyzed yet. Open a pull request on {repository.full_name} to see it here.
          </div>
        )}
        {pullRequests?.results.map((pr) => (
          <div
            key={pr.id}
            onClick={() => navigate(`/github/pull-requests/${pr.id}`)}
            style={{
              display: 'grid', gridTemplateColumns: GRID_COLUMNS, padding: '16px 20px',
              borderTop: '1px solid var(--color-border)', fontSize: 14, alignItems: 'center', cursor: 'pointer',
            }}
          >
            <span>
              {pr.title || `Pull request #${pr.pull_request_number}`}
              <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginTop: 2 }}>
                {pr.author} · {formatDate(pr.created_at)}
              </div>
            </span>
            <span style={{ color: 'var(--color-text-secondary-2)' }}>#{pr.pull_request_number}</span>
            <span style={{ fontWeight: 600, color: scoreColor(pr.overall_score) }}>{formatScore(pr.overall_score)}</span>
            <span style={{ color: pr.status === 'failed' ? 'var(--color-danger)' : 'var(--color-success)' }}>
              {STATUS_LABEL[pr.status] || pr.status}
            </span>
          </div>
        ))}
      </div>

      {pullRequests?.count > pageSize && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 16, marginTop: 20 }}>
          <button className="btn btn-outline" style={{ padding: '8px 16px', fontSize: 13 }} disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            ← Previous
          </button>
          <span style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>Page {page} of {totalPages}</span>
          <button className="btn btn-outline" style={{ padding: '8px 16px', fontSize: 13 }} disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            Next →
          </button>
        </div>
      )}
    </div>
  )
}

function PageHeader({ repository }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
      <div>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>Pull requests</div>
        <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
          {repository ? (
            <>
              AI code review results for <span style={{ fontFamily: 'var(--font-mono)' }}>{repository.full_name}</span>.
            </>
          ) : (
            'AI code review results for your monitored repository.'
          )}
        </div>
      </div>
      <Link to="/github" style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>← Manage repositories</Link>
    </div>
  )
}

function StatCard({ label, value }) {
  return (
    <div className="card" style={{ padding: 22 }}>
      <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, marginTop: 6, textTransform: 'capitalize' }}>{value}</div>
    </div>
  )
}

function TrendsChart({ trends }) {
  if (trends === null) {
    return <div style={{ fontSize: 13, color: 'var(--color-text-muted)', marginTop: 16 }}>Loading…</div>
  }
  if (trends.length === 0) {
    return (
      <div style={{ fontSize: 13, color: 'var(--color-text-muted)', marginTop: 16 }}>
        No completed pull request analyses in this window yet.
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 140, marginTop: 20 }}>
      {trends.map((point) => (
        <div
          key={point.date}
          title={`${formatDate(point.date)}: ${Math.round(point.average_score)}% avg over ${point.prs_count} PR${point.prs_count === 1 ? '' : 's'}`}
          style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', height: '100%', minWidth: 4 }}
        >
          <div
            style={{
              width: '100%',
              height: `${Math.max(2, point.average_score)}%`,
              background: scoreColor(point.average_score),
              borderRadius: '3px 3px 0 0',
            }}
          />
        </div>
      ))}
    </div>
  )
}
