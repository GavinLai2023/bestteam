import { afterEach, describe, expect, it } from 'vitest'
import { setLanguage } from './i18n'

afterEach(() => {
  setLanguage('en')
})

describe('document language sync', () => {
  it('sets <html lang> to the active language on init', () => {
    // setup.ts already imported lib/i18n before this test file's own imports
    // run, with an empty jsdom localStorage -- so this reflects the real
    // init-time default, not a value this test set up itself.
    expect(document.documentElement.lang).toBe('en')
  })

  it('updates <html lang> when the language is switched', () => {
    setLanguage('zh-CN')

    expect(document.documentElement.lang).toBe('zh-CN')
  })
})
