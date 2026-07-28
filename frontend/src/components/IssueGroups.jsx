import { severityMeta } from '../lib/format'

// Shared by anything that renders a FileAnalysis-shaped issue list (PR file
// review, on-demand single-file check) - both produce issues via
// pr_analysis_service._analyze_file_content, so they share this exact shape.
export function groupIssuesBySource(issues) {
  return {
    general: issues.filter((i) => i.source !== 'security' && i.source !== 'performance'),
    performance: issues.filter((i) => i.source === 'performance'),
    security: issues.filter((i) => i.source === 'security'),
  }
}

export function IssueGroupList({ issues }) {
  if (issues.length === 0) {
    return <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>No issues found.</div>
  }
  const { general, performance, security } = groupIssuesBySource(issues)
  const groupCount = [general, performance, security].filter((g) => g.length > 0).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {general.length > 0 && <IssueGroup title={groupCount > 1 ? 'General' : null} issues={general} />}
      {performance.length > 0 && <IssueGroup title="Performance" issues={performance} />}
      {security.length > 0 && <IssueGroup title="Security" issues={security} />}
    </div>
  )
}

export function IssueGroup({ title, issues }) {
  return (
    <div>
      {title && (
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary-2)', textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 8 }}>
          {title}
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {issues.map((issue, i) => {
          const meta = severityMeta(issue.severity)
          return (
            <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
              <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 100, color: meta.color, background: 'var(--color-bg-subtle)', whiteSpace: 'nowrap', marginTop: 1 }}>
                {meta.label}
              </span>
              <div>
                <div style={{ fontSize: 13 }}>
                  {issue.message}
                  {issue.line != null && <span style={{ color: 'var(--color-text-secondary-2)' }}> — line {issue.line}</span>}
                </div>
                {issue.explanation && (
                  <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginTop: 3 }}>{issue.explanation}</div>
                )}
                {issue.remediation && (
                  <div style={{ fontSize: 12, color: 'var(--color-text-secondary-2)', marginTop: 3 }}>
                    <strong style={{ color: 'var(--color-text-secondary)' }}>Fix: </strong>{issue.remediation}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
