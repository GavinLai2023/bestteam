import { afterEach, describe, it, expect } from 'vitest'
import { endOfLocalDay, formatDateTime } from './dateFormat'
import { setLanguage } from './i18n'

afterEach(() => {
  setLanguage('en')
})

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

  it('uses the Chinese locale format when the UI language is Chinese', () => {
    setLanguage('zh-CN')
    const date = new Date(2026, 6, 31, 14, 55)
    expect(formatDateTime(date)).toBe('2026年7月31日 14:55')
  })
})

describe('endOfLocalDay', () => {
  it('returns the last millisecond of the chosen local day', () => {
    const end = endOfLocalDay('2030-01-02')
    expect(end.getTime()).toBe(new Date(2030, 0, 3).getTime() - 1)
    expect(end.getDate()).toBe(2)
  })

  it('rolls over month and year boundaries correctly', () => {
    expect(endOfLocalDay('2030-12-31').getTime()).toBe(new Date(2031, 0, 1).getTime() - 1)
  })
})
