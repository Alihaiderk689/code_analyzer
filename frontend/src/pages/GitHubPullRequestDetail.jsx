import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getGitHubPullRequest } from '../lib/resources'
import { formatScore, formatDateTime, scoreColor, STATUS_LABEL } from '../lib/format'
import { IssueGroupList } from '../components/IssueGroups'
import { ApiError } from '../lib/api'

export default function GitHubPullRequestDetail() {
  const { id } = useParams()
  const [pr, setPr] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getGitHubPullRequest(id)
      .then(setPr)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this pull request.'))
  }, [id])

  if (error) {
    return (
      <div style={{ maxWidth: 900, margin: '0 auto', padding: '44px 40px 100px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (!pr) {
    return <div className="page-loading">Loading…</div>
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '44px 40px 100px' }}>
      <Link to="/github/pull-requests" style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>← All pull requests</Link>

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginTop: 12 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 28, fontWeight: 500 }}>
            {pr.title || `Pull request #${pr.pull_request_number}`}
          </div>
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 6 }}>
            <span style={{ fontFamily: 'var(--font-mono)' }}>{pr.repository_name}</span> #{pr.pull_request_number} · {pr.author} · {formatDateTime(pr.created_at)}
          </div>
        </div>
        <span
          style={{
            fontSize: 12, padding: '5px 12px', borderRadius: 100, whiteSpace: 'nowrap',
            color: pr.status === 'failed' ? 'var(--color-danger)' : 'var(--color-success)',
            background: 'var(--color-bg-subtle)',
          }}
        >
          {STATUS_LABEL[pr.status] || pr.status}
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 18, marginTop: 24 }}>
        <MetricCard label="Overall score" value={`${formatScore(pr.overall_score)}${pr.overall_score != null ? '%' : ''}`} color={scoreColor(pr.overall_score)} />
        <MetricCard label="Files analyzed" value={pr.file_analyses.length} color="var(--color-info)" />
        <MetricCard label="Review posted" value={pr.review_posted ? 'Yes' : 'No'} color={pr.review_posted ? 'var(--color-success)' : 'var(--color-text-secondary-2)'} />
      </div>

      {pr.summary && (
        <div className="card" style={{ marginTop: 20, padding: 20, fontSize: 14, lineHeight: 1.6 }}>{pr.summary}</div>
      )}

      {pr.status === 'failed' && pr.error && (
        <div className="msg-error" style={{ marginTop: 20 }}>{pr.error}</div>
      )}

      <div style={{ fontWeight: 600, fontSize: 15, marginTop: 32 }}>Files</div>
      {pr.file_analyses.length === 0 ? (
        <div className="card" style={{ marginTop: 12, padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>
          No supported files were changed in this pull request.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 12 }}>
          {pr.file_analyses.map((file) => <FileCard key={file.id} file={file} />)}
        </div>
      )}
    </div>
  )
}

function FileCard({ file }) {
  return (
    <div className="card" style={{ padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 14 }}>{file.file_path}</span>
          <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 100, background: 'var(--color-bg-subtle)', color: 'var(--color-text-secondary-2)' }}>
            {file.language}
          </span>
        </div>
        <span style={{ fontSize: 13, fontWeight: 600, color: scoreColor(file.score) }}>{formatScore(file.score)}</span>
      </div>

      <div style={{ marginTop: 14 }}>
        <IssueGroupList issues={file.issues} />
      </div>
    </div>
  )
}

function MetricCard({ label, value, color }) {
  return (
    <div className="card" style={{ padding: 20 }}>
      <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, marginTop: 6, color }}>{value}</div>
    </div>
  )
}
