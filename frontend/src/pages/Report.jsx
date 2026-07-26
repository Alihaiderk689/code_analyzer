import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { diffLines } from 'diff'
import {
  getAnalysis,
  getSuggestions,
  getExplanation,
  getRefactor,
  downloadReportPdf,
  downloadReportJson,
  getReportHtml,
  reanalyzeAnalysis,
  deleteAnalysis,
} from '../lib/resources'
import { scoreColor, formatScore, issueMeta, STATUS_LABEL } from '../lib/format'
import { ApiError } from '../lib/api'

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

const TABS = [
  { key: 'issues', label: 'Issues' },
  { key: 'suggestions', label: 'AI Suggestions' },
  { key: 'explanation', label: 'AI Explanation' },
  { key: 'refactored', label: 'Refactored Code' },
]

export default function Report() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [analysis, setAnalysis] = useState(null)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [tab, setTab] = useState('issues')
  const [toast, setToast] = useState(null)
  const [reanalyzing, setReanalyzing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    getAnalysis(id)
      .then(setAnalysis)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this report.'))
  }, [id])

  const handleReanalyze = async () => {
    setReanalyzing(true)
    setActionError('')
    try {
      const result = await reanalyzeAnalysis(id)
      setAnalysis(result)
      setTab('issues')
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not reanalyze.')
    } finally {
      setReanalyzing(false)
    }
  }

  const handleDelete = async () => {
    if (!window.confirm(`Delete "${analysis.name}"? This cannot be undone.`)) return
    setDeleting(true)
    setActionError('')
    try {
      await deleteAnalysis(id)
      navigate('/history')
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not delete this analysis.')
      setDeleting(false)
    }
  }

  const handleDownload = async (format) => {
    setToast(`Generating ${format.toUpperCase()} report…`)
    try {
      const blob = format === 'pdf' ? await downloadReportPdf(id) : await downloadReportJson(id)
      triggerBlobDownload(blob, `analysis-${id}-report.${format}`)
      setToast('Report downloaded.')
    } catch {
      setToast(`Could not generate the ${format.toUpperCase()} report.`)
    } finally {
      setTimeout(() => setToast(null), 2800)
    }
  }

  const handleViewHtml = async () => {
    try {
      const blob = await getReportHtml(id)
      const url = URL.createObjectURL(blob)
      window.open(url, '_blank')
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not load the HTML report.')
    }
  }

  const handleEdit = () => {
    const originalSource = sessionStorage.getItem(`ca_source_${id}`)
    if (!originalSource) {
      setActionError(
        "Original source isn't available for editing in this session (the server doesn't return it after submission) — only sessions that submitted it can edit it."
      )
      return
    }
    sessionStorage.setItem('ca_pending_snippet', originalSource)
    sessionStorage.setItem('ca_pending_analysis_name', analysis.name)
    navigate('/analyze')
  }

  if (error) {
    return (
      <div style={{ maxWidth: 1320, margin: '0 auto', padding: '40px 40px 100px' }}>
        <div className="msg-error">{error}</div>
      </div>
    )
  }

  if (!analysis) return <div className="page-loading">Loading report…</div>

  const isCompleted = analysis.status === 'completed'

  return (
    <div style={{ maxWidth: 1320, margin: '0 auto', padding: '40px 40px 100px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)' }}>
            {analysis.language}
          </div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 30, fontWeight: 500, marginTop: 4 }}>
            {analysis.name}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button className="btn btn-outline" style={{ padding: '12px 18px', fontSize: 14 }} onClick={handleEdit}>
            Edit code
          </button>
          <button
            className="btn btn-outline"
            style={{ padding: '12px 18px', fontSize: 14 }}
            disabled={reanalyzing}
            onClick={handleReanalyze}
          >
            {reanalyzing ? 'Reanalyzing…' : 'Reanalyze'}
          </button>

          <div style={{ display: 'flex', border: '1px solid var(--color-border-2)', borderRadius: 100, overflow: 'hidden' }}>
            <button
              className="btn-ghost"
              style={{ padding: '12px 16px', fontSize: 13, borderRight: '1px solid var(--color-border-2)' }}
              onClick={() => handleDownload('pdf')}
            >
              ↓ PDF
            </button>
            <button
              className="btn-ghost"
              style={{ padding: '12px 16px', fontSize: 13, borderRight: '1px solid var(--color-border-2)' }}
              onClick={() => handleDownload('json')}
            >
              ↓ JSON
            </button>
            <button className="btn-ghost" style={{ padding: '12px 16px', fontSize: 13 }} onClick={handleViewHtml}>
              View HTML
            </button>
          </div>

          <button
            className="btn"
            style={{ padding: '12px 20px', fontSize: 14, background: 'var(--color-danger)', color: '#fff' }}
            disabled={deleting}
            onClick={handleDelete}
          >
            {deleting ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>

      {toast && (
        <div className="msg-success" style={{ marginTop: 14, width: 'fit-content' }}>
          {toast}
        </div>
      )}
      {actionError && (
        <div className="msg-error" style={{ marginTop: 14, width: 'fit-content' }}>
          {actionError}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 18, marginTop: 28 }}>
        <MetricCard label="Quality score" value={`${formatScore(analysis.quality_score)}%`} pct={analysis.quality_score ?? 0} color={scoreColor(analysis.quality_score)} />
        <MetricCard label="Lines of code" value={analysis.lines_of_code} pct={100} color="#2563eb" />
        <MetricCard label="Issues found" value={analysis.issues_count} pct={Math.min(100, analysis.issues_count * 10)} color="#D97706" />
        <MetricCard label="Status" value={STATUS_LABEL[analysis.status] || analysis.status} pct={isCompleted ? 100 : 40} color={isCompleted ? '#3fa54c' : '#8c8c85'} />
      </div>

      <div style={{ display: 'flex', gap: 6, marginTop: 32, borderBottom: '1px solid var(--color-border)', overflowX: 'auto' }}>
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
              whiteSpace: 'nowrap',
              color: tab === t.key ? '#171717' : '#8c8c85',
              borderBottom: `2px solid ${tab === t.key ? '#171717' : 'transparent'}`,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 22 }}>
        {tab === 'issues' && <IssuesTab analysis={analysis} />}
        {tab === 'suggestions' && <AiListTab id={id} isCompleted={isCompleted} loader={getSuggestions} field="suggestions" />}
        {tab === 'explanation' && <AiTextTab id={id} isCompleted={isCompleted} loader={getExplanation} field="explanation" />}
        {tab === 'refactored' && <RefactorTab id={id} isCompleted={isCompleted} />}
      </div>

      <div style={{ marginTop: 36 }}>
        <Link to="/dashboard" style={{ fontSize: 14, color: 'var(--color-text-secondary)' }}>
          ← Back to dashboard
        </Link>
      </div>
    </div>
  )
}

function MetricCard({ label, value, pct, color }) {
  return (
    <div className="card" style={{ padding: 18 }}>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, marginTop: 6 }}>{value}</div>
      <div style={{ height: 6, background: '#f0f0ed', borderRadius: 100, marginTop: 10 }}>
        <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, pct))}%`, background: color, borderRadius: 100 }} />
      </div>
    </div>
  )
}

function IssuesTab({ analysis }) {
  const issues = analysis.issues || []
  if (analysis.status !== 'completed') {
    return <EmptyNote>Issues will appear once this analysis finishes running.</EmptyNote>
  }
  if (issues.length === 0) {
    return <EmptyNote>No issues found. Clean code.</EmptyNote>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {issues.map((issue, i) => {
        const meta = issueMeta(issue.type)
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, border: '1px solid var(--color-border)', borderRadius: 12, padding: '14px 16px' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: meta.color, marginTop: 6, flexShrink: 0 }} />
            <div>
              <div style={{ fontSize: 14 }}>{issue.message}</div>
              <div style={{ fontSize: 12, color: 'var(--color-text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>
                {meta.label}
                {issue.line != null ? ` · line ${issue.line}` : ''}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function useLazyAi(id, isCompleted, loader) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = async (regenerate = false) => {
    setBusy(true)
    setErr('')
    try {
      setData(await loader(id, regenerate))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'AI service is currently unavailable.')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (isCompleted && data === null && !busy) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isCompleted])

  return { data, busy, err, regenerate: () => load(true) }
}

function AiListTab({ id, isCompleted, loader }) {
  const { data, busy, err, regenerate } = useLazyAi(id, isCompleted, loader)

  if (!isCompleted) return <EmptyNote>Available once this analysis finishes running.</EmptyNote>
  if (busy && !data) return <EmptyNote>Generating AI suggestions…</EmptyNote>
  if (err) return <div className="msg-error">{err}</div>

  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {(data?.suggestions || []).map((text, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, border: '1px solid var(--color-border)', borderRadius: 12, padding: '14px 16px' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#059669', marginTop: 6, flexShrink: 0 }} />
            <div style={{ fontSize: 14 }}>{text}</div>
          </div>
        ))}
        {data?.suggestions?.length === 0 && <EmptyNote>No suggestions returned.</EmptyNote>}
      </div>
      <RegenerateButton busy={busy} onClick={regenerate} />
    </div>
  )
}

function AiTextTab({ id, isCompleted, loader }) {
  const { data, busy, err, regenerate } = useLazyAi(id, isCompleted, loader)

  if (!isCompleted) return <EmptyNote>Available once this analysis finishes running.</EmptyNote>
  if (busy && !data) return <EmptyNote>Generating explanation…</EmptyNote>
  if (err) return <div className="msg-error">{err}</div>

  return (
    <div>
      <div style={{ fontSize: 14, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{data?.explanation}</div>
      <RegenerateButton busy={busy} onClick={regenerate} />
    </div>
  )
}

function splitPartLines(value) {
  return value.replace(/\n$/, '').split('\n')
}

// Builds two line arrays kept in row-for-row sync, the way a side-by-side diff
// view should: a removed block and the added block that replaces it are padded
// with blank placeholder rows on the shorter side, so unchanged code below them
// doesn't drift out of alignment between the two panels.
function buildDiffLines(before, after) {
  const parts = diffLines(before || '', after || '')
  const beforeLines = []
  const afterLines = []

  let i = 0
  while (i < parts.length) {
    const part = parts[i]

    if (!part.added && !part.removed) {
      splitPartLines(part.value).forEach((text) => {
        beforeLines.push({ text, changed: false })
        afterLines.push({ text, changed: false })
      })
      i++
      continue
    }

    let removed = []
    let added = []
    if (part.removed) {
      removed = splitPartLines(part.value)
      i++
      if (parts[i]?.added) {
        added = splitPartLines(parts[i].value)
        i++
      }
    } else {
      added = splitPartLines(part.value)
      i++
    }

    const rows = Math.max(removed.length, added.length)
    for (let j = 0; j < rows; j++) {
      beforeLines.push(j < removed.length ? { text: removed[j], changed: true } : { text: '', empty: true })
      afterLines.push(j < added.length ? { text: added[j], changed: true } : { text: '', empty: true })
    }
  }

  return { beforeLines, afterLines }
}

function RefactorTab({ id, isCompleted }) {
  const { data, busy, err, regenerate } = useLazyAi(id, isCompleted, getRefactor)
  const originalSource = sessionStorage.getItem(`ca_source_${id}`)

  if (!isCompleted) return <EmptyNote>Available once this analysis finishes running.</EmptyNote>
  if (busy && !data) return <EmptyNote>Generating refactored code…</EmptyNote>
  if (err) return <div className="msg-error">{err}</div>

  const diff = originalSource ? buildDiffLines(originalSource, data?.refactored_code || '') : null
  const changes = data?.explanation || []

  return (
    <div>
      {!originalSource && (
        <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 12 }}>
          Original source isn't available for this session (the server doesn't return it after submission) — showing
          the AI-refactored version only.
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: originalSource ? '1fr 1fr' : '1fr', gap: 16, minWidth: 0 }}>
        {diff && (
          <div style={{ minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)' }}>BEFORE</span>
              <CopyButton text={originalSource} />
            </div>
            <DiffCodeBox lines={diff.beforeLines} changedBg="rgba(220,38,38,0.1)" changedPrefix="−" border="var(--color-border)" />
          </div>
        )}
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--color-success)', fontFamily: 'var(--font-mono)' }}>AFTER</span>
            <CopyButton text={data?.refactored_code} />
          </div>
          {diff ? (
            <DiffCodeBox lines={diff.afterLines} changedBg="rgba(63,165,76,0.15)" changedPrefix="+" border="#c8ecb8" />
          ) : (
            <pre style={codeBoxStyle('#fafaf8', '#c8ecb8')}>{data?.refactored_code}</pre>
          )}
        </div>
      </div>
      {changes.length > 0 && (
        <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 14 }}>What changed, and why</div>
          {changes.map((change, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, border: '1px solid var(--color-border)', borderRadius: 12, padding: '14px 16px' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#059669', marginTop: 6, flexShrink: 0 }} />
              <div>
                <div style={{ fontSize: 14 }}>{change.summary}</div>
                {change.benefit && (
                  <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginTop: 4 }}>{change.benefit}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      <RegenerateButton busy={busy} onClick={regenerate} />
    </div>
  )
}

function DiffCodeBox({ lines, changedBg, changedPrefix, border }) {
  return (
    <div style={{ ...codeBoxStyle('#fff', border), padding: '8px 0', overflowX: 'auto', overflowY: 'hidden' }}>
      <div style={{ minWidth: '100%', width: 'max-content' }}>
        {lines.map((line, i) => (
          <div
            key={i}
            style={{
              background: line.empty ? 'rgba(0,0,0,0.03)' : line.changed ? changedBg : 'transparent',
              padding: '0 16px',
              whiteSpace: 'pre',
            }}
          >
            {line.empty ? (
              ' '
            ) : (
              <>
                <span style={{ opacity: line.changed ? 1 : 0, color: line.changed ? undefined : 'transparent', marginRight: 8 }}>
                  {changedPrefix}
                </span>
                {line.text || String.fromCharCode(32)}
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function codeBoxStyle(bg, border) {
  return {
    background: bg,
    border: `1px solid ${border}`,
    borderRadius: 12,
    padding: 16,
    fontFamily: 'var(--font-mono)',
    fontSize: 13,
    lineHeight: 1.6,
    margin: 0,
    whiteSpace: 'pre-wrap',
    overflowX: 'auto',
  }
}

function RegenerateButton({ busy, onClick }) {
  return (
    <button className="btn btn-outline" style={{ marginTop: 16, fontSize: 13, padding: '9px 16px' }} disabled={busy} onClick={onClick}>
      {busy ? 'Regenerating…' : 'Regenerate'}
    </button>
  )
}

function EmptyNote({ children }) {
  return <div style={{ fontSize: 14, color: 'var(--color-text-muted)', padding: '20px 0' }}>{children}</div>
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard API can fail (permissions/insecure context) - not worth surfacing an error for.
    }
  }

  return (
    <button
      onClick={handleCopy}
      disabled={!text}
      title="Copy code"
      style={{
        border: '1px solid var(--color-border-2)',
        background: '#fff',
        borderRadius: 8,
        width: 26,
        height: 26,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: text ? 'pointer' : 'default',
        padding: 0,
        color: copied ? 'var(--color-success)' : 'var(--color-text-secondary-2)',
        flexShrink: 0,
      }}
    >
      {copied ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  )
}
