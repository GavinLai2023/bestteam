import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WizardProgress from './WizardProgress'
import type { BuilderSession } from '../lib/types'

const sessionWithSpec = (): BuilderSession => ({
  id: 's1',
  status: 'spec',
  intent_text: 'reply to customer emails',
  specification_json: { name: 'support_workflow', agents: [], teams: [] },
  updated_at: '2026-08-09T00:00:00Z',
})

const renderBar = (busy?: boolean) =>
  render(
    <MemoryRouter initialEntries={['/wizard/s1/confirm']}>
      <WizardProgress session={sessionWithSpec()} busy={busy} />
    </MemoryRouter>,
  )

describe('WizardProgress', () => {
  it('links to a reachable step the customer is not currently on', () => {
    renderBar()

    expect(screen.getByText('Go live').closest('a')).not.toBeNull()
  })

  it('shows six steps with the interview between challenge and documents', () => {
    renderBar()

    const labels = screen.getAllByText(/./, { selector: '.wizard-step-label' }).map((el) => el.textContent)
    expect(labels).toEqual([
      'Challenge',
      'Questions',
      'Documents',
      'Your team',
      'Confirm',
      'Go live',
    ])
  })

  it('links the questions step once a session exists', () => {
    renderBar()

    expect(screen.getByText('Questions').closest('a')).not.toBeNull()
  })

  it('locks the questions step without a session', () => {
    render(
      <MemoryRouter initialEntries={['/wizard']}>
        <WizardProgress session={null} />
      </MemoryRouter>,
    )

    expect(screen.getByText('Questions').closest('a')).toBeNull()
  })

  // Leaving mid-update is how a customer publishes a team they have not seen:
  // "Go live" unlocks on the spec existing, so it stays lit while the
  // Architect is redesigning that very spec.
  it('offers no step links while a stage page is working', () => {
    renderBar(true)

    expect(screen.getByText('Go live').closest('a')).toBeNull()
    expect(screen.getByText('Challenge').closest('a')).toBeNull()
  })
})
