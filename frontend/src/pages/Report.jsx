import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  getAnalysis,
  downloadReportPdf,
  downloadReportJson,
  getReportHtml,
  reanalyzeAnalysis,
  deleteAnalysis,
} from '../lib/resources'
import { scoreColor, formatScore, STATUS_LABEL } from '../lib/format'
import AnalysisTabs from '../components/AnalysisTabs'
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

export default function Report() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [analysis, setAnalysis] = useState(null)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [tabsResetKey, setTabsResetKey] = useState(0)
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
      setTabsResetKey((k) => k + 1)
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
        <MetricCard label="Lines of code" value={analysis.lines_of_code} pct={100} color="var(--color-info)" />
        <MetricCard label="Issues found" value={analysis.issues_count} pct={Math.min(100, analysis.issues_count * 10)} color="var(--color-warning)" />
        <MetricCard label="Status" value={STATUS_LABEL[analysis.status] || analysis.status} pct={isCompleted ? 100 : 40} color={isCompleted ? 'var(--color-success)' : 'var(--color-text-secondary-2)'} />
      </div>

      <div style={{ marginTop: 32 }}>
        <AnalysisTabs
          key={tabsResetKey}
          analysisId={id}
          issues={analysis.issues}
          isCompleted={isCompleted}
          originalSource={sessionStorage.getItem(`ca_source_${id}`)}
          headerRight={
            isCompleted && (
              <Link
                to={`/report/${id}/chat`}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  marginBottom: 8,
                  padding: '8px 14px',
                  fontSize: 13,
                  fontWeight: 500,
                  whiteSpace: 'nowrap',
                  color: 'var(--color-text)',
                  background: 'var(--color-bg-subtle)',
                  border: '1px solid var(--color-border-2)',
                  borderRadius: 100,
                  textDecoration: 'none',
                }}
              >
                💬 Chat with your code
              </Link>
            )
          }
        />
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
      <div style={{ height: 6, background: 'var(--color-bg-subtle)', borderRadius: 100, marginTop: 10 }}>
        <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, pct))}%`, background: color, borderRadius: 100 }} />
      </div>
    </div>
  )
}
