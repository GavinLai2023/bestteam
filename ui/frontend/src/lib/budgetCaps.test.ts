import { describe, it, expect } from 'vitest'
import { parseCap } from './budgetCaps'

describe('parseCap', () => {
  it('reads a whole number', () => {
    expect(parseCap('25')).toBe(25)
  })

  it('reads a decimal amount', () => {
    expect(parseCap('12.5')).toBe(12.5)
  })

  it('ignores surrounding spaces', () => {
    expect(parseCap('  40  ')).toBe(40)
  })

  it('returns null for an empty box -- no cap', () => {
    expect(parseCap('')).toBeNull()
    expect(parseCap('   ')).toBeNull()
  })

  it('keeps an explicit zero, which is a cap of zero and not "no cap"', () => {
    // 0 means automation off. Turning it into null would silently do the
    // opposite of what was typed; the API rejects it, and that is its job.
    expect(parseCap('0')).toBe(0)
  })

  it('returns undefined, not null, for something that is not a number', () => {
    // null would reach the API as "remove this cap": `JSON.stringify(NaN)` is
    // `null`, so a NaN here would silently delete a customer's spend limit.
    expect(parseCap('abc')).toBeUndefined()
  })

  it('returns undefined for a figure too large to be a number', () => {
    expect(parseCap('1e999')).toBeUndefined()
  })

  it('returns undefined for the words that parse to a non-finite number', () => {
    expect(parseCap('Infinity')).toBeUndefined()
    expect(parseCap('-Infinity')).toBeUndefined()
    expect(parseCap('NaN')).toBeUndefined()
  })
})
