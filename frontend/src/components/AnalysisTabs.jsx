import { useEffect, useState } from 'react'
import { diffLines } from 'diff'
import { getSuggestions, getExplanation, getRefactor } from '../lib/resources'
import { issueMeta } from '../lib/format'
import { IssueGroupList } from './IssueGroups'
import { ApiError } from '../lib/api'

const TABS = [
  { key: 'issues', label: 'Issues' },
  { key: 'source', label: 'Source Code' },
  { key: 'suggestions', label: 'AI Suggestions' },
  { key: 'explanation', label: 'AI Explanation' },
  { key: 'refactored', label: 'Refactored Code' },
]

// Issues/Suggestions/Explanation/Refactor/Source tabs shared by the paste-and-
// upload Report page and the GitHub on-demand file check - both produce a
// real Analysis row (see analyses.analysis_views / github_integration
// .repository_views._create_analysis_for_file_check), so the same generic
// /analysis/<id>/... AI endpoints work unmodified for either caller.
export default function AnalysisTabs({ analysisId, issues, isCompleted, originalSource, headerRight }) {
  const [tab, setTab] = useState('issues')

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
          borderBottom: '1px solid var(--color-border)',
          overflowX: 'auto',
        }}
      >
        <div style={{ display: 'flex', gap: 6 }}>
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
                color: tab === t.key ? 'var(--color-text)' : 'var(--color-text-secondary-2)',
                borderBottom: `2px solid ${tab === t.key ? 'var(--color-text)' : 'transparent'}`,
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
        {headerRight}
      </div>

      <div style={{ marginTop: 22 }}>
        {tab === 'issues' && <IssuesTab issues={issues} isCompleted={isCompleted} />}
        {tab === 'source' && <SourceTab source={originalSource} />}
        {tab === 'suggestions' && <AiListTab id={analysisId} isCompleted={isCompleted} loader={getSuggestions} />}
        {tab === 'explanation' && <AiTextTab id={analysisId} isCompleted={isCompleted} loader={getExplanation} />}
        {tab === 'refactored' && <RefactorTab id={analysisId} isCompleted={isCompleted} originalSource={originalSource} />}
      </div>
    </div>
  )
}

function IssuesTab({ issues, isCompleted }) {
  const list = issues || []
  if (!isCompleted) {
    return <EmptyNote>Issues will appear once this analysis finishes running.</EmptyNote>
  }
  if (list.length === 0) {
    return <EmptyNote>No issues found. Clean code.</EmptyNote>
  }

  // GitHub file-check issues carry severity/source (see IssueGroups.jsx);
  // plain paste/upload issues from analyses.engine.analyze_code don't - each
  // shape gets the rendering it was designed for, rather than forcing the
  // richer one and showing blank severity badges for the plain shape.
  if (list[0].severity !== undefined) {
    return <IssueGroupList issues={list} />
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {list.map((issue, i) => {
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

function SourceTab({ source }) {
  if (!source) {
    return (
      <EmptyNote>
        Original source isn't available for this session (the server doesn't return it after submission).
      </EmptyNote>
    )
  }
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
        <CopyButton text={source} />
      </div>
      <pre style={codeBoxStyle('var(--color-bg-subtle)', 'var(--color-border)')}>{source}</pre>
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

  const suggestions = data?.suggestions || []
  const securitySuggestions = suggestions.filter((s) => s.category === 'security')
  const generalSuggestions = suggestions.filter((s) => s.category !== 'security')

  return (
    <div>
      {generalSuggestions.length > 0 && (
        <SuggestionGroup
          // Only label the general group when there's a security group below it
          // to distinguish from - a lone list of suggestions doesn't need a heading.
          title={securitySuggestions.length > 0 ? 'General' : null}
          items={generalSuggestions}
          dotColor="var(--color-code-string)"
        />
      )}
      {securitySuggestions.length > 0 && (
        <SuggestionGroup title="Security" items={securitySuggestions} dotColor="var(--color-danger)" />
      )}
      {suggestions.length === 0 && <EmptyNote>No suggestions returned.</EmptyNote>}
      <RegenerateButton busy={busy} onClick={regenerate} />
    </div>
  )
}

function SuggestionGroup({ title, items, dotColor }) {
  return (
    <div style={{ marginBottom: 18 }}>
      {title && (
        <div
          style={{
            fontWeight: 600,
            fontSize: 13,
            color: dotColor,
            marginBottom: 10,
            textTransform: 'uppercase',
            letterSpacing: '0.03em',
          }}
        >
          {title}
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {items.map((item, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, border: '1px solid var(--color-border)', borderRadius: 12, padding: '14px 16px' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: dotColor, marginTop: 6, flexShrink: 0 }} />
            <div style={{ fontSize: 14 }}>{item.text}</div>
          </div>
        ))}
      </div>
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

function RefactorTab({ id, isCompleted, originalSource }) {
  const { data, busy, err, regenerate } = useLazyAi(id, isCompleted, getRefactor)

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
            <DiffCodeBox lines={diff.beforeLines} changedBg="var(--color-diff-removed-bg)" changedPrefix="−" border="var(--color-border)" />
          </div>
        )}
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--color-success)', fontFamily: 'var(--font-mono)' }}>AFTER</span>
            <CopyButton text={data?.refactored_code} />
          </div>
          {diff ? (
            <DiffCodeBox lines={diff.afterLines} changedBg="var(--color-diff-added-bg)" changedPrefix="+" border="var(--color-diff-added-border)" />
          ) : (
            <pre style={codeBoxStyle('var(--color-bg-subtle)', 'var(--color-diff-added-border)')}>{data?.refactored_code}</pre>
          )}
        </div>
      </div>
      {changes.length > 0 && (
        <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 14 }}>What changed, and why</div>
          {changes.map((change, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, border: '1px solid var(--color-border)', borderRadius: 12, padding: '14px 16px' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--color-code-string)', marginTop: 6, flexShrink: 0 }} />
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
    <div style={{ ...codeBoxStyle('var(--color-surface)', border), padding: '8px 0', overflowX: 'auto', overflowY: 'hidden' }}>
      <div style={{ minWidth: '100%', width: 'max-content' }}>
        {lines.map((line, i) => (
          <div
            key={i}
            style={{
              background: line.empty ? 'var(--color-diff-empty-bg)' : line.changed ? changedBg : 'transparent',
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
        background: 'var(--color-surface)',
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
