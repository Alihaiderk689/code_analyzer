import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  getGitHubStatus,
  getGitHubLoginUrl,
  disconnectGitHub,
  listGitHubRepositories,
  listMonitoredGitHubRepositories,
  selectGitHubRepository,
  deselectGitHubRepository,
} from '../lib/resources'
import { formatDate } from '../lib/format'
import { ApiError } from '../lib/api'

const CALLBACK_ERROR_MESSAGE = {
  access_denied: 'GitHub authorization was cancelled.',
  missing_code: 'GitHub did not return an authorization code. Please try again.',
  invalid_state: 'The GitHub authorization link expired or was invalid. Please try again.',
  not_configured: 'GitHub integration is not configured on the server yet.',
  github_error: 'GitHub returned an error while connecting your account.',
}

export default function GitHub() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [status, setStatus] = useState(null)
  const [available, setAvailable] = useState(null)
  const [monitored, setMonitored] = useState(null)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [connecting, setConnecting] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [busyRepoId, setBusyRepoId] = useState(null)

  const callbackNotice = searchParams.get('connected') === 'true'
    ? { kind: 'success', text: 'GitHub account connected.' }
    : searchParams.get('error')
      ? { kind: 'error', text: CALLBACK_ERROR_MESSAGE[searchParams.get('error')] || 'Could not connect your GitHub account.' }
      : null

  useEffect(() => {
    if (searchParams.get('connected') || searchParams.get('error')) {
      setSearchParams({}, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadRepositories = () =>
    Promise.all([listGitHubRepositories(), listMonitoredGitHubRepositories()]).then(([availableRes, monitoredRes]) => {
      setAvailable(availableRes.results)
      setMonitored(monitoredRes)
    })

  useEffect(() => {
    getGitHubStatus()
      .then((data) => {
        setStatus(data)
        if (data.connected) return loadRepositories()
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load your GitHub connection.'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleConnect = async () => {
    setConnecting(true)
    setActionError('')
    try {
      const { authorize_url } = await getGitHubLoginUrl()
      window.location.href = authorize_url
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not start GitHub authorization.')
      setConnecting(false)
    }
  }

  const handleDisconnect = async () => {
    if (!window.confirm(`Disconnect GitHub account "${status.username}"? Monitored repositories will stop being analyzed.`)) return
    setDisconnecting(true)
    setActionError('')
    try {
      await disconnectGitHub()
      setStatus({ connected: false })
      setAvailable(null)
      setMonitored(null)
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not disconnect your GitHub account.')
    } finally {
      setDisconnecting(false)
    }
  }

  const handleToggleMonitor = async (repo) => {
    setBusyRepoId(repo.repository_id)
    setActionError('')
    try {
      if (repo.is_monitored) {
        const monitoredRow = monitored.find((m) => m.repository_id === repo.repository_id)
        if (monitoredRow) await deselectGitHubRepository(monitoredRow.id)
      } else {
        await selectGitHubRepository(repo.repository_id, repo.full_name)
      }
      await loadRepositories()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not update this repository.')
    } finally {
      setBusyRepoId(null)
    }
  }

  if (error) {
    return (
      <div style={{ maxWidth: 900, margin: '0 auto', padding: '44px 40px 100px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (!status) {
    return <div className="page-loading">Loading…</div>
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '44px 40px 100px' }}>
      <div>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>GitHub integration</div>
        <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
          Connect a repository to get automatic AI code review on every pull request.
        </div>
      </div>

      {callbackNotice && (
        <div className={callbackNotice.kind === 'success' ? 'msg-success' : 'msg-error'} style={{ marginTop: 20 }}>
          {callbackNotice.text}
        </div>
      )}
      {actionError && <div className="msg-error" style={{ marginTop: 20 }}>{actionError}</div>}

      <div className="card" style={{ marginTop: 24, padding: 24 }}>
        {!status.connected ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 15 }}>No GitHub account connected</div>
              <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
                We'll ask for access to read your repositories and manage webhooks so we can review pull requests.
              </div>
            </div>
            <button className="btn btn-dark" style={{ padding: '11px 20px', fontSize: 14, whiteSpace: 'nowrap' }} disabled={connecting} onClick={handleConnect}>
              {connecting ? 'Redirecting…' : 'Connect GitHub'}
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div
                style={{
                  width: 36, height: 36, borderRadius: '50%', background: 'var(--color-accent)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14, fontWeight: 700,
                }}
              >
                {status.username.charAt(0).toUpperCase()}
              </div>
              <div>
                <div style={{ fontWeight: 600, fontSize: 15 }}>Connected as {status.username}</div>
                <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 2 }}>
                  Since {formatDate(status.connected_at)}
                  {status.token_invalid && <span style={{ color: 'var(--color-danger)' }}> · Connection expired, please reconnect</span>}
                </div>
              </div>
            </div>
            <button className="btn btn-outline" style={{ padding: '9px 16px', fontSize: 13 }} disabled={disconnecting} onClick={handleDisconnect}>
              {disconnecting ? 'Disconnecting…' : 'Disconnect'}
            </button>
          </div>
        )}
      </div>

      {status.connected && (
        <>
          <div style={{ fontWeight: 600, fontSize: 15, marginTop: 32 }}>Connected repositories</div>
          <div className="card" style={{ marginTop: 12, overflow: 'hidden' }}>
            {monitored === null && <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
            {monitored?.length === 0 && (
              <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>
                No repositories monitored yet — pick one below.
              </div>
            )}
            {monitored?.map((repo) => (
              <div
                key={repo.id}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '14px 20px', borderTop: monitored.indexOf(repo) === 0 ? 'none' : '1px solid var(--color-border)',
                }}
              >
                <div>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 14 }}>{repo.full_name}</span>
                  <span style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginLeft: 10 }}>{repo.default_branch}</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button
                    className="btn btn-outline"
                    style={{ padding: '6px 14px', fontSize: 12 }}
                    onClick={() => navigate(`/github/repositories/${repo.id}/files`)}
                  >
                    Browse files
                  </button>
                  <button
                    className="btn btn-outline"
                    style={{ padding: '6px 14px', fontSize: 12 }}
                    disabled={busyRepoId === repo.repository_id}
                    onClick={() => handleToggleMonitor({ repository_id: repo.repository_id, full_name: repo.full_name, is_monitored: true })}
                  >
                    Stop monitoring
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div style={{ fontWeight: 600, fontSize: 15, marginTop: 32 }}>Your GitHub repositories</div>
          <div className="card" style={{ marginTop: 12, overflow: 'hidden' }}>
            {available === null && <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>Loading…</div>}
            {available?.length === 0 && (
              <div style={{ padding: 20, fontSize: 14, color: 'var(--color-text-muted)' }}>No repositories found on your GitHub account.</div>
            )}
            {available?.map((repo, i) => (
              <div
                key={repo.repository_id}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '14px 20px', borderTop: i === 0 ? 'none' : '1px solid var(--color-border)',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 14 }}>{repo.full_name}</span>
                  {repo.private && (
                    <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 100, background: 'var(--color-bg-subtle)', color: 'var(--color-text-secondary-2)' }}>
                      Private
                    </span>
                  )}
                </div>
                <button
                  className={repo.is_monitored ? 'btn btn-outline' : 'btn btn-dark'}
                  style={{ padding: '6px 14px', fontSize: 12 }}
                  disabled={busyRepoId === repo.repository_id}
                  onClick={() => handleToggleMonitor(repo)}
                >
                  {busyRepoId === repo.repository_id ? 'Working…' : repo.is_monitored ? 'Stop monitoring' : 'Monitor'}
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 20 }}>
        Already reviewed a pull request? <Link to="/github/pull-requests">See recent pull requests</Link>.
      </div>
    </div>
  )
}
