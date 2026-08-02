import { describe, it, expect } from 'vitest'
import { formatDateTime } from './dateFormat'

describe('formatDateTime', () => {
  it('formats a date as "DD MMM YYYY, h:mm AM/PM"', () => {
    const date = new Date(2026, 6, 31, 14, 55) // 31 Jul 2026, 2:55 PM local time
    expect(formatDateTime(date)).toBe('31 JUL 2026, 2:55 PM')
  })

  it('pads a single-digit day with a leading zero', () => {
    const date = new Date(2026, 0, 5, 9, 5) // 5 Jan 2026, 9:05 AM local time
    expect(formatDateTime(date)).toBe('05 JAN 2026, 9:05 AM')
  })

  it('accepts anything new Date() accepts, e.g. an ISO string', () => {
    const date = new Date(2026, 11, 1, 0, 0)
    expect(formatDateTime(date.toISOString())).toBe(formatDateTime(date))
  })
})
