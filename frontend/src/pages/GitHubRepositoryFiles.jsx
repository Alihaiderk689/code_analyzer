import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  getGitHubRepositoryTree, getFileCheckQuota, getContextCheckQuota, getGitHubFileContent,
  analyzeGitHubFile, analyzeGitHubFileWithContext,
  getGitHubRepositoryIndexStatus, rebuildGitHubRepositoryIndex,
} from '../lib/resources'
import { formatScore, scoreColor } from '../lib/format'
import AnalysisTabs from '../components/AnalysisTabs'
import { IssueGroupList } from '../components/IssueGroups'
import { useCountdown, formatCountdown } from '../lib/useCountdown'
import { ApiError } from '../lib/api'

// While the dependency-graph build (see backend repo_index_service.py) is
// pending/running, poll for completion so the "Understanding repository..."
// banner clears on its own once analysis can actually use it as context.
const INDEX_POLL_INTERVAL_MS = 4000

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

const RELATION_LABEL = {
  imports: 'This file imports it',
  imported_by: 'Imports this file',
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
  const [contextQuota, setContextQuota] = useState(null)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [selectedPath, setSelectedPath] = useState(null)
  // The file's raw source, fetched for free the moment it's clicked - distinct
  // from fileResult/contextResult below, which only exist once the user opts
  // into spending a check by pressing "Analyze" or "Analyze with repo context".
  const [fileContent, setFileContent] = useState(null)
  const [fileContentLoading, setFileContentLoading] = useState(false)
  const [fileContentError, setFileContentError] = useState('')
  const [fileResult, setFileResult] = useState(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')
  // The file plus its direct dependency-graph neighbors (imports/importers),
  // each analyzed - see backend PRAnalysisService.analyze_file_with_context.
  // Tracked under its own quota (contextQuota), separate from the plain
  // single-file check above.
  const [contextResult, setContextResult] = useState(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [contextError, setContextError] = useState('')
  const [indexStatus, setIndexStatus] = useState(null)
  const [reindexing, setReindexing] = useState(false)
  const [reindexTrigger, setReindexTrigger] = useState(0)
  const pollTimeoutRef = useRef(null)

  const refreshQuota = () => getFileCheckQuota().then(setQuota).catch(() => {})
  const refreshContextQuota = () => getContextCheckQuota().then(setContextQuota).catch(() => {})

  useEffect(() => {
    let cancelled = false

    const poll = () => {
      clearTimeout(pollTimeoutRef.current)
      getGitHubRepositoryIndexStatus(pk)
        .then((data) => {
          if (cancelled) return
          setIndexStatus(data)
          if (data.status === 'pending' || data.status === 'running') {
            pollTimeoutRef.current = setTimeout(poll, INDEX_POLL_INTERVAL_MS)
          }
        })
        .catch(() => {})
    }
    poll()

    return () => {
      cancelled = true
      clearTimeout(pollTimeoutRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pk, reindexTrigger])

  const handleRebuildIndex = async () => {
    setReindexing(true)
    try {
      await rebuildGitHubRepositoryIndex(pk)
      setIndexStatus({ status: 'pending' })
      setReindexTrigger((n) => n + 1)  // re-runs the polling effect above
    } catch {
      // best-effort - the status banner just won't update if this failed to queue
    } finally {
      setReindexing(false)
    }
  }

  useEffect(() => {
    getGitHubRepositoryTree(pk)
      .then(setTree)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this repository.'))

    // Surface today's already-completed check right away, instead of only
    // after re-clicking the same file - it should stay visible for the rest
    // of the day, across reloads, not just right after analyzing it. Guarded
    // by repository_id because each quota is global per user, not per-repo: a
    // check from a previously-monitored (now inactive) repo must never get
    // attributed to whichever repo is open right now. Fetched together (not
    // as two separate .then()s) so which one "wins" the initial selection
    // doesn't depend on network timing - the richer context check wins if
    // both somehow exist for today.
    Promise.all([
      getFileCheckQuota().catch(() => null),
      getContextCheckQuota().catch(() => null),
    ]).then(([fileQuota, ctxQuota]) => {
      if (fileQuota) setQuota(fileQuota)
      if (ctxQuota) setContextQuota(ctxQuota)

      const fileHit = fileQuota?.today_check?.repository_id === Number(pk) ? fileQuota.today_check : null
      const ctxHit = ctxQuota?.today_check?.repository_id === Number(pk) ? ctxQuota.today_check : null

      if (ctxHit) {
        setSelectedPath(ctxHit.path)
        setContextResult({ ...ctxHit, cached: true })
        setExpanded(new Set(ancestorPaths(ctxHit.path)))
      } else if (fileHit) {
        setSelectedPath(fileHit.path)
        setFileResult({ ...fileHit, cached: true })
        setExpanded(new Set(ancestorPaths(fileHit.path)))
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pk])

  const root = useMemo(() => (tree ? buildTree(tree.results) : null), [tree])
  const msUntilReset = useCountdown(quota?.reset_at, refreshQuota)
  const msUntilContextReset = useCountdown(contextQuota?.reset_at, refreshContextQuota)

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
    setContextError('')
    setFileResult(null)
    setContextResult(null)
    setFileContent(null)
    setFileContentError('')

    // Re-opening today's already-checked file is free (see backend
    // RepositoryFileAnalyzeView/RepositoryFileContextAnalyzeView) - go
    // straight to showing that result again instead of making the user press
    // "Analyze" a second time for something that doesn't cost them anything.
    const isTodaysContextCheck = contextQuota?.today_check?.repository_id === Number(pk) && contextQuota.today_check.path === path
    if (isTodaysContextCheck) {
      setContextLoading(true)
      try {
        setContextResult(await analyzeGitHubFileWithContext(pk, path))
      } catch (err) {
        setContextError(err instanceof ApiError ? err.message : 'Could not analyze this file.')
      } finally {
        setContextLoading(false)
      }
      return
    }

    const isTodaysFileCheck = quota?.today_check?.repository_id === Number(pk) && quota.today_check.path === path
    if (isTodaysFileCheck) {
      setFileLoading(true)
      try {
        setFileResult(await analyzeGitHubFile(pk, path))
      } catch (err) {
        setFileError(err instanceof ApiError ? err.message : 'Could not analyze this file.')
      } finally {
        setFileLoading(false)
      }
      return
    }

    setFileContentLoading(true)
    try {
      setFileContent(await getGitHubFileContent(pk, path))
    } catch (err) {
      setFileContentError(err instanceof ApiError ? err.message : 'Could not load this file.')
    } finally {
      setFileContentLoading(false)
    }
  }

  const handleAnalyze = async () => {
    if (!selectedPath) return
    setFileError('')
    setFileLoading(true)
    try {
      setFileResult(await analyzeGitHubFile(pk, selectedPath))
    } catch (err) {
      setFileError(err instanceof ApiError ? err.message : 'Could not analyze this file.')
    } finally {
      setFileLoading(false)
      refreshQuota()
    }
  }

  const handleAnalyzeWithContext = async () => {
    if (!selectedPath) return
    setContextError('')
    setContextLoading(true)
    try {
      setContextResult(await analyzeGitHubFileWithContext(pk, selectedPath))
    } catch (err) {
      setContextError(err instanceof ApiError ? err.message : 'Could not analyze this file.')
    } finally {
      setContextLoading(false)
      refreshContextQuota()
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
        <div style={{ textAlign: 'right', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {quota && (
            <div>
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
          {contextQuota && (
            <div>
              <div style={{ fontSize: 13, fontFamily: 'var(--font-mono)' }}>
                {contextQuota.remaining}/{contextQuota.limit} repo-context check{contextQuota.limit === 1 ? '' : 's'} left today
              </div>
              {contextQuota.remaining <= 0 && (
                <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginTop: 2 }}>
                  Resets in {msUntilContextReset != null ? formatCountdown(msUntilContextReset) : '—'}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <RepoIndexBanner status={indexStatus} onRebuild={handleRebuildIndex} rebuilding={reindexing} />

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
              Select a file from the tree to view it. Analyzing it costs {quota?.limit ?? 1} file check per day (or
              {' '}{contextQuota?.limit ?? 1} to also analyze its related files) — re-opening the same file today is
              free, but a different file will have to wait for the reset.
            </div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600 }}>{selectedPath}</div>
                {!fileResult && !contextResult && fileContent && !fileContent.skipped && (
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button
                      className="btn btn-outline"
                      style={{ padding: '7px 16px', fontSize: 13, whiteSpace: 'nowrap' }}
                      onClick={handleAnalyze}
                      disabled={fileLoading || contextLoading}
                    >
                      {fileLoading ? 'Analyzing…' : 'Analyze'}
                    </button>
                    <button
                      className="btn btn-dark"
                      style={{ padding: '7px 16px', fontSize: 13, whiteSpace: 'nowrap' }}
                      onClick={handleAnalyzeWithContext}
                      disabled={fileLoading || contextLoading}
                      title="Also analyzes the files this one imports and the files that import it, using the repository's dependency graph"
                    >
                      {contextLoading ? 'Analyzing…' : 'Analyze with repo context'}
                    </button>
                  </div>
                )}
              </div>
              <div style={{ marginTop: 16 }}>
                {contextResult ? (
                  <ContextResultView result={contextResult} />
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
                ) : fileLoading || contextLoading ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--color-text-muted)', fontSize: 13 }}>
                    <span className="spinner" /> Analyzing…
                  </div>
                ) : fileError || contextError ? (
                  <div className="msg-error">{fileError || contextError}</div>
                ) : fileContentLoading ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--color-text-muted)', fontSize: 13 }}>
                    <span className="spinner" /> Loading file…
                  </div>
                ) : fileContentError ? (
                  <div className="msg-error">{fileContentError}</div>
                ) : fileContent?.skipped ? (
                  <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                    {SKIP_REASON_LABEL[fileContent.skip_reason] || "This file can't be analyzed."}
                  </div>
                ) : fileContent ? (
                  <pre
                    style={{
                      background: 'var(--color-bg-subtle)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 12,
                      padding: 16,
                      fontFamily: 'var(--font-mono)',
                      fontSize: 13,
                      lineHeight: 1.6,
                      margin: 0,
                      whiteSpace: 'pre-wrap',
                      overflowX: 'auto',
                      maxHeight: '60vh',
                      overflowY: 'auto',
                    }}
                  >
                    {fileContent.content}
                  </pre>
                ) : null}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// Result of "Analyze with repo context" - the primary file (same AnalysisTabs
// view as a plain single-file check) plus a "Related files" section for its
// direct dependency-graph neighbors (see backend PRAnalysisService
// .analyze_file_with_context). Related files only ever get a lightweight
// issues/score summary, not their own AnalysisTabs/chat - keeps a single
// context check bounded in cost.
function ContextResultView({ result }) {
  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 20, fontWeight: 600, color: scoreColor(result.score) }}>{formatScore(result.score)}</span>
        <span style={{ fontSize: 12, padding: '2px 8px', borderRadius: 100, background: 'var(--color-bg-subtle)', color: 'var(--color-text-secondary-2)' }}>
          {result.language}
        </span>
        {result.cached && (
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary-2)' }}>Already checked today</span>
        )}
      </div>
      <div style={{ marginTop: 20 }}>
        <AnalysisTabs
          key={result.analysis_id}
          analysisId={result.analysis_id}
          issues={result.issues}
          isCompleted
          originalSource={result.content}
          headerRight={
            result.analysis_id && (
              <Link to={`/report/${result.analysis_id}/chat`} style={PILL_LINK_STYLE}>
                💬 Chat with your code
              </Link>
            )
          }
        />
      </div>

      <div style={{ marginTop: 32 }}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 4 }}>Related files</div>
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginBottom: 16 }}>
          Files this one imports, and files that import it - from the repository's dependency graph.
        </div>
        {result.related.length === 0 ? (
          <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
            No related files found - either the repository index hasn't finished building yet, or this file has no
            tracked imports/importers.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {result.related.map((rel) => (
              <div key={rel.path} style={{ border: '1px solid var(--color-border)', borderRadius: 12, padding: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 600 }}>{rel.path}</span>
                  <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 100, background: 'var(--color-bg-subtle)', color: 'var(--color-text-secondary-2)' }}>
                    {RELATION_LABEL[rel.relation] || rel.relation}
                  </span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: scoreColor(rel.score), marginLeft: 'auto' }}>
                    {formatScore(rel.score)}
                  </span>
                </div>
                <div style={{ marginTop: 12 }}>
                  <IssueGroupList issues={rel.issues} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

// Reflects the dependency-graph build (repo_index_service.py) that runs
// automatically after a repo is selected - purely informational, analyzing a
// file works with or without it (see analyze_file_by_path's repo_context,
// which degrades to nothing if there's no completed index yet).
function RepoIndexBanner({ status, onRebuild, rebuilding }) {
  if (!status || status.status === 'not_started') return null

  const baseStyle = {
    marginTop: 16,
    padding: '10px 14px',
    borderRadius: 8,
    fontSize: 13,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    background: 'var(--color-bg-subtle)',
    border: '1px solid var(--color-border-2)',
  }

  if (status.status === 'pending' || status.status === 'running') {
    return (
      <div style={baseStyle}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-text-secondary-2)' }}>
          <span className="spinner" />
          Understanding repository structure{status.files_total ? ` (${status.files_indexed}/${status.files_total} files)` : '…'}
        </span>
      </div>
    )
  }

  if (status.status === 'failed') {
    return (
      <div style={baseStyle}>
        <span style={{ color: 'var(--color-danger)' }}>Couldn't understand this repository's structure.</span>
        <button className="btn btn-outline" style={{ padding: '5px 12px', fontSize: 12 }} onClick={onRebuild} disabled={rebuilding}>
          {rebuilding ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    )
  }

  // completed
  return (
    <div style={baseStyle}>
      <span style={{ color: 'var(--color-text-secondary-2)' }}>
        Understands {status.files_indexed} file{status.files_indexed === 1 ? '' : 's'} and how they connect - analysis
        results use this for better recommendations.
        {status.truncated && ' (Repository is large - only part of it was indexed.)'}
      </span>
      <button className="btn btn-outline" style={{ padding: '5px 12px', fontSize: 12, whiteSpace: 'nowrap' }} onClick={onRebuild} disabled={rebuilding}>
        {rebuilding ? 'Rebuilding…' : 'Rebuild'}
      </button>
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
