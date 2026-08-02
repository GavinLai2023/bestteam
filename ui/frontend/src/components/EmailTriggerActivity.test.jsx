import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import EmailTriggerActivity from './EmailTriggerActivity'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getEmailTrigger: vi.fn(),
  },
}))

describe('EmailTriggerActivity', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a distinct empty state when no trigger has ever been configured', async () => {
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'off' })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/no automatic runs configured yet/i)).toBeInTheDocument()
  })

  it('shows an Off status card (not a blank tab) when a configured trigger is turned off', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: false,
      workflow_name: 'Automated_Email_Responder_Team',
      status: 'off',
      last_checked_at: '2026-07-31T04:55:53Z',
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Off')).toBeInTheDocument()
    expect(screen.getByText(/Automated_Email_Responder_Team/)).toBeInTheDocument()
    expect(screen.getByText(/Last checked/)).toBeInTheDocument()
  })

  it('shows the last-checked time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      last_checked_at: '2026-07-31T14:55:00',
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/31 JUL 2026, 2:55 PM/)).toBeInTheDocument()
  })

  it('shows an Active status card when watching for mail', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Active')).toBeInTheDocument()
  })

  it.each(['disabled', 'paused_cap'])('shows a Paused status card for backend status %s', async (status) => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status,
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Paused')).toBeInTheDocument()
  })

  it('shows a Problem status card and the error message when the mailbox check is failing', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'error',
      last_checked_at: null,
      last_error: "Couldn't connect to the mailbox.",
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Problem')).toBeInTheDocument()
    expect(screen.getByText("Couldn't connect to the mailbox.")).toBeInTheDocument()
  })

  it('does not list individual recent runs -- that duplicate belongs on the Runs tab', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    await screen.findByText('Active')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('clicking "View automatic runs" invokes the onViewRuns callback', async () => {
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      last_checked_at: null,
      last_error: null,
    })
    const onViewRuns = vi.fn()

    render(<EmailTriggerActivity onViewRuns={onViewRuns} />)

    const button = await screen.findByText('View automatic runs')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(onViewRuns).toHaveBeenCalledTimes(1)
  })

  it("couldn't load status shows an error banner", async () => {
    api.getEmailTrigger.mockRejectedValue(new Error('boom'))

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/Couldn't load automatic-run status/)).toBeInTheDocument()
  })
})
