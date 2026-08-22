import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import LanguageSelect from './LanguageSelect'
import { setLanguage } from '../lib/i18n'

afterEach(() => {
  setLanguage('en')
})

describe('LanguageSelect', () => {
  it('offers every supported language, each labelled in its own language', () => {
    render(<LanguageSelect />)
    const select = screen.getByRole('combobox', { name: /language/i })
    expect(select).toHaveValue('en')
    expect(screen.getByRole('option', { name: 'English' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '中文' })).toBeInTheDocument()
  })

  it('switches the active language', () => {
    render(<LanguageSelect />)
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    expect(document.documentElement.lang).toBe('zh-CN')
  })
})
