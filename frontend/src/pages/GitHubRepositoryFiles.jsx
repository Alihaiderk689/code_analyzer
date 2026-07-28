import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getGitHubRepositoryTree, getFileCheckQuota, analyzeGitHubFile } from '../lib/resources'
import { formatScore, scoreColor } from '../lib/format'
import AnalysisTabs from '../components/AnalysisTabs'
import { useCountdown, formatCountdown } from '../lib/useCountdown'
import { ApiError } from '../lib/api'

// Matches the "Chat with your code" pill on Report.jsx, since these links lead
// into that exact same page (the file check is backed by a real Analysis row -
// see repository_views._create_analysis_for_file_check).
const PILL_LINK_STYLE = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '8px 14px',
  fontSize: 13,
  fontWeight: 500,
  whiteSpace: 'nowrap',
  color: 'var(--color-text)',
  background: 'var(--color-bg-subtle)',
  border: '1px solid var(--color-border-2)',
  borderRadius: 100,
  textDecoration: 'none',
}

const SKIP_REASON_LABEL = {
  binary: "This is a binary file and can't be analyzed.",
  lock_file: "Lock files aren't analyzed.",
  generated: 'Generated/vendored files are skipped.',
  unsupported_language: "This file's language isn't supported yet.",
  too_large: 'This file is too large to analyze.',
  removed: 'This file no longer exists.',
  fetch_failed: 'Could not fetch this file from GitHub.',
}

function buildTree(entries) {
  const root = { name: '', path: '', type: 'dir', children: {} }
  for (const entry of entries) {
    const parts = entry.path.split('/')
    let node = root
    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1
      if (!node.children[part]) {
        node.children[part] = {
          name: part,
          path: parts.slice(0, i + 1).join('/'),
          type: isLast ? entry.type : 'dir',
          children: {},
        }
      }
      node = node.children[part]
    })
  }
  return root
}

function sortedChildren(node) {
  return Object.values(node.children).sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

// Every directory on the way down to `path`, so it can be pre-expanded -
// "backend/accounts/models.py" -> ["backend", "backend/accounts"].
function ancestorPaths(path) {
  const parts = path.split('/').slice(0, -1)
  return parts.map((_, i) => parts.slice(0, i + 1).join('/'))
}

export default function GitHubRepositoryFiles() {
  const { pk } = useParams()
  const [tree, setTree] = useState(null)
  const [quota, setQuota] = useState(null)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [selectedPath, setSelectedPath] = useState(null)
  const [fileResult, setFileResult] = useState(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')

  const refreshQuota = () => getFileCheckQuota().then(setQuota).catch(() => {})

  useEffect(() => {
    getGitHubRepositoryTree(pk)
      .then(setTree)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this repository.'))

    getFileCheckQuota()
      .then((data) => {
        setQuota(data)
        // Surface today's already-completed check right away, instead of only
        // after re-clicking the same file - it should stay visible for the
        // rest of the day, across reloads, not just right after analyzing it.
        // Guarded by repository_id because the quota is global per user, not
        // per-repo: a check from a previously-monitored (now inactive) repo
        // must never get attributed to whichever repo is open right now.
        if (data.today_check && data.today_check.repository_id === Number(pk)) {
          setSelectedPath(data.today_check.path)
          setFileResult({ ...data.today_check, cached: true })
          setExpanded(new Set(ancestorPaths(data.today_check.path)))
        }
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pk])

  const root = useMemo(() => (tree ? buildTree(tree.results) : null), [tree])
  const msUntilReset = useCountdown(quota?.reset_at, refreshQuota)

  const handleToggleDir = (path) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const handleSelectFile = async (path) => {
    setSelectedPath(path)
    setFileError('')
    setFileResult(null)
    setFileLoading(true)
    try {
      setFileResult(await analyzeGitHubFile(pk, path))
    } catch (err) {
      setFileError(err instanceof ApiError ? err.message : 'Could not analyze this file.')
    } finally {
      setFileLoading(false)
      refreshQuota()
    }
  }

  if (error) {
    return (
      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '44px 40px 100px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (!tree || !root) {
    return <div className="page-loading">Loading…</div>
  }

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '44px 40px 100px' }}>
      <Link to="/github" style={{ fontSize: 13, color: 'var(--color-text-secondary-2)' }}>← Manage repositories</Link>

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginTop: 12 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 28, fontWeight: 500 }}>Browse files</div>
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginTop: 6 }}>
            <span style={{ fontFamily: 'var(--font-mono)' }}>{tree.repository}</span> · {tree.default_branch}
          </div>
        </div>
        {quota && (
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 13, fontFamily: 'var(--font-mono)' }}>
              {quota.remaining}/{quota.limit} file check{quota.limit === 1 ? '' : 's'} left today
            </div>
            {quota.remaining <= 0 && (
              <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginTop: 2 }}>
                Resets in {msUntilReset != null ? formatCountdown(msUntilReset) : '—'}
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 24, marginTop: 24, alignItems: 'start' }}>
        <div className="card" style={{ padding: '10px 6px', maxHeight: '70vh', overflowY: 'auto' }}>
          {sortedChildren(root).length === 0 ? (
            <div style={{ padding: '10px 12px', fontSize: 13, color: 'var(--color-text-muted)' }}>This repository is empty.</div>
          ) : (
            sortedChildren(root).map((child) => (
              <TreeNode
                key={child.path}
                node={child}
                depth={0}
                expanded={expanded}
                onToggleDir={handleToggleDir}
                selectedPath={selectedPath}
                onSelectFile={handleSelectFile}
              />
            ))
          )}
        </div>

        <div className="card" style={{ padding: 24, minHeight: 300 }}>
          {!selectedPath ? (
            <div style={{ fontSize: 14, color: 'var(--color-text-muted)' }}>
              Select a file from the tree to analyze it. You get {quota?.limit ?? 1} file check per day — re-opening the same
              file today is free, but a different file will have to wait for the reset.
            </div>
          ) : (
            <>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600 }}>{selectedPath}</div>
              <div style={{ marginTop: 16 }}>
                {fileLoading ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--color-text-muted)', fontSize: 13 }}>
                    <span className="spinner" /> Analyzing…
                  </div>
                ) : fileError ? (
                  <div className="msg-error">{fileError}</div>
                ) : fileResult?.skipped ? (
                  <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                    {SKIP_REASON_LABEL[fileResult.skip_reason] || "This file can't be analyzed."}
                  </div>
                ) : fileResult ? (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                      <span style={{ fontSize: 20, fontWeight: 600, color: scoreColor(fileResult.score) }}>{formatScore(fileResult.score)}</span>
                      <span style={{ fontSize: 12, padding: '2px 8px', borderRadius: 100, background: 'var(--color-bg-subtle)', color: 'var(--color-text-secondary-2)' }}>
                        {fileResult.language}
                      </span>
                      {fileResult.cached && (
                        <span style={{ fontSize: 12, color: 'var(--color-text-secondary-2)' }}>Already checked today</span>
                      )}
                    </div>
                    <div style={{ marginTop: 20 }}>
                      <AnalysisTabs
                        key={fileResult.analysis_id}
                        analysisId={fileResult.analysis_id}
                        issues={fileResult.issues}
                        isCompleted
                        originalSource={fileResult.content}
                        headerRight={
                          fileResult.analysis_id && (
                            <Link to={`/report/${fileResult.analysis_id}/chat`} style={PILL_LINK_STYLE}>
                              💬 Chat with your code
                            </Link>
                          )
                        }
                      />
                    </div>
                  </>
                ) : null}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function TreeNode({ node, depth, expanded, onToggleDir, selectedPath, onSelectFile }) {
  const paddingLeft = 10 + depth * 16

  if (node.type === 'dir') {
    const isExpanded = expanded.has(node.path)
    return (
      <div>
        <div
          onClick={() => onToggleDir(node.path)}
          style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', paddingLeft, cursor: 'pointer', fontSize: 13, fontWeight: 600, borderRadius: 6 }}
        >
          <span style={{ width: 10, display: 'inline-block', color: 'var(--color-text-secondary-2)' }}>{isExpanded ? '▾' : '▸'}</span>
          {node.name}
        </div>
        {isExpanded && sortedChildren(node).map((child) => (
          <TreeNode
            key={child.path}
            node={child}
            depth={depth + 1}
            expanded={expanded}
            onToggleDir={onToggleDir}
            selectedPath={selectedPath}
            onSelectFile={onSelectFile}
          />
        ))}
      </div>
    )
  }

  const isSelected = node.path === selectedPath
  return (
    <div
      onClick={() => onSelectFile(node.path)}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', paddingLeft: paddingLeft + 18,
        cursor: 'pointer', fontSize: 13, fontFamily: 'var(--font-mono)', borderRadius: 6,
        background: isSelected ? 'var(--color-bg-subtle)' : 'transparent',
      }}
    >
      {node.name}
    </div>
  )
}
