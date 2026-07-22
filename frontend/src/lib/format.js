export function scoreColor(score) {
  if (score == null) return 'var(--color-text-muted)'
  if (score >= 85) return '#3fa54c'
  if (score >= 70) return '#D97706'
  return '#DC2626'
}

export function formatScore(score) {
  return score == null ? '—' : Math.round(score)
}

export function formatDate(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export const STATUS_LABEL = {
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export const ISSUE_TYPE = {
  todo: { label: 'TODO marker', color: '#D97706' },
  long_line: { label: 'Long line', color: '#2563EB' },
  no_comments: { label: 'No comments', color: '#8c8c85' },
  syntax_error: { label: 'Syntax error', color: '#DC2626' },
  undefined_name: { label: 'Undefined name', color: '#E11D48' },
  undefined_export: { label: 'Undefined export', color: '#E11D48' },
  duplicate_argument: { label: 'Duplicate argument', color: '#DC2626' },
  import_star_used: { label: 'Wildcard import', color: '#D97706' },
  redefined_while_unused: { label: 'Redefined before use', color: '#D97706' },
  unused_variable: { label: 'Unused variable', color: '#8c8c85' },
  unused_import: { label: 'Unused import', color: '#8c8c85' },
  runtime_error: { label: 'Runtime error', color: '#DC2626' },
  execution_timeout: { label: 'Timed out', color: '#D97706' },
}

export function issueMeta(type) {
  return ISSUE_TYPE[type] || { label: type, color: '#8c8c85' }
}
