import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import WizardBusyNotice from './WizardBusyNotice'

describe('WizardBusyNotice', () => {
  it('announces the wait to assistive tech', () => {
    render(<WizardBusyNotice />)

    expect(screen.getByRole('status')).toHaveTextContent('Please stay on this page')
  })

  it('carries a moving indicator, hidden from assistive tech', () => {
    const { container } = render(<WizardBusyNotice />)

    const pulse = container.querySelector('.wizard-busy-pulse')
    expect(pulse).not.toBeNull()
    expect(pulse).toHaveAttribute('aria-hidden', 'true')
  })
})
