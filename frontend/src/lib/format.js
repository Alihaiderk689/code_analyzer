export function scoreColor(score) {
  if (score == null) return 'var(--color-text-muted)'
  if (score >= 85) return 'var(--color-success)'
  if (score >= 70) return 'var(--color-warning)'
  return 'var(--color-danger)'
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
  todo: { label: 'TODO marker', color: 'var(--color-warning)' },
  long_line: { label: 'Long line', color: 'var(--color-info)' },
  no_comments: { label: 'No comments', color: 'var(--color-text-secondary-2)' },
  syntax_error: { label: 'Syntax error', color: 'var(--color-danger)' },
  undefined_name: { label: 'Undefined name', color: 'var(--color-danger-2)' },
  undefined_export: { label: 'Undefined export', color: 'var(--color-danger-2)' },
  duplicate_argument: { label: 'Duplicate argument', color: 'var(--color-danger)' },
  import_star_used: { label: 'Wildcard import', color: 'var(--color-warning)' },
  redefined_while_unused: { label: 'Redefined before use', color: 'var(--color-warning)' },
  unused_variable: { label: 'Unused variable', color: 'var(--color-text-secondary-2)' },
  unused_import: { label: 'Unused import', color: 'var(--color-text-secondary-2)' },
  runtime_error: { label: 'Runtime error', color: 'var(--color-danger)' },
  execution_timeout: { label: 'Timed out', color: 'var(--color-warning)' },
}

export function issueMeta(type) {
  return ISSUE_TYPE[type] || { label: type, color: 'var(--color-text-secondary-2)' }
}
