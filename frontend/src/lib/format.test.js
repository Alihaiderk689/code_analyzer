import { describe, expect, it } from 'vitest'
import { formatDate, formatScore, issueMeta, scoreColor } from './format'

describe('scoreColor', () => {
  it('returns muted color for null/undefined score', () => {
    expect(scoreColor(null)).toBe('var(--color-text-muted)')
    expect(scoreColor(undefined)).toBe('var(--color-text-muted)')
  })

  it('returns green at and above the 85 threshold', () => {
    expect(scoreColor(85)).toBe('#3fa54c')
    expect(scoreColor(100)).toBe('#3fa54c')
  })

  it('returns amber between the 70 and 85 thresholds', () => {
    expect(scoreColor(70)).toBe('#D97706')
    expect(scoreColor(84)).toBe('#D97706')
  })

  it('returns red below the 70 threshold', () => {
    expect(scoreColor(69)).toBe('#DC2626')
    expect(scoreColor(0)).toBe('#DC2626')
  })
})

describe('formatScore', () => {
  it('renders an em dash for null/undefined', () => {
    expect(formatScore(null)).toBe('—')
    expect(formatScore(undefined)).toBe('—')
  })

  it('rounds to the nearest integer', () => {
    expect(formatScore(87.6)).toBe(88)
    expect(formatScore(87.4)).toBe(87)
  })

  it('does not treat zero as missing', () => {
    expect(formatScore(0)).toBe(0)
  })
})

describe('formatDate', () => {
  it('returns an empty string for falsy input', () => {
    expect(formatDate(null)).toBe('')
    expect(formatDate('')).toBe('')
  })

  it('formats an ISO date as "Mon D"', () => {
    expect(formatDate('2026-03-05T12:00:00Z')).toMatch(/^Mar 5$/)
  })
})

describe('issueMeta', () => {
  it('returns known metadata for a recognized issue type', () => {
    expect(issueMeta('todo')).toEqual({ label: 'TODO marker', color: '#D97706' })
  })

  it('falls back to the raw type as the label for unknown issue types', () => {
    expect(issueMeta('something_new')).toEqual({ label: 'something_new', color: '#8c8c85' })
  })
})
