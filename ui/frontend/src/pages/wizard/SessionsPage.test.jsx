import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionsPage from './SessionsPage'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({
  api: {
    listSessions: vi.fn(),
    getEmailTrigger: vi.fn(),
  },
}))

const renderPage = () =>
  render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  )

const session = (overrides = {}) => ({
  id: 's1',
  status: 'deployed',
  intent_text: 'do stuff',
  specification_json: { name: 'my-team' },
  updated_at: '2026-07-31T00:00:00Z',
  ...overrides,
})

describe('SessionsPage automation tag', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a one-line automation tag on the matching team card when the trigger is enabled', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'my-team',
      status: 'active',
      last_checked_at: '2026-07-31T11:02:00Z',
    })

    renderPage()

    expect(await screen.findByText(/Automation on/)).toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger targets a different team', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'some-other-team',
      status: 'active',
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation on/)).not.toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger is disabled', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })

  it('does not render the old full automatic-runs history block', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'my-team',
      status: 'active',
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automatic runs —/)).not.toBeInTheDocument()
  })

  it('degrades gracefully (no tag, no crash) if fetching the trigger fails', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockRejectedValue(new Error('boom'))

    renderPage()

    expect(await screen.findByText('my-team')).toBeInTheDocument()
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })
})
