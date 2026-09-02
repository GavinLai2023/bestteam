import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import EmailTriggerActivity from './EmailTriggerActivity'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getEmailTrigger: vi.fn(),
    listFilteredMessages: vi.fn(),
    releaseFilteredMessage: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const ACTIVE_TRIGGER = {
  enabled: true,
  pipeline_name: 'wf-a',
  status: 'active' as const,
  daily_cap: 0,
  last_checked_at: null,
  last_error: null,
}

describe('EmailTriggerActivity', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listFilteredMessages.mockResolvedValue({ filtered: [] })
    mockedApi.releaseFilteredMessage.mockResolvedValue({ released: true })
  })

  it('shows a distinct empty state when no trigger has ever been configured', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'off', daily_cap: 0 })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/no automatic runs configured yet/i)).toBeInTheDocument()
  })

  it('shows an Off status card (not a blank tab) when a configured trigger is turned off', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: false,
      pipeline_name: 'Automated_Email_Responder_Team',
      status: 'off',
      daily_cap: 0,
      last_checked_at: '2026-07-31T04:55:53Z',
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Off')).toBeInTheDocument()
    expect(screen.getByText(/Automated_Email_Responder_Team/)).toBeInTheDocument()
    expect(screen.getByText(/Last checked/)).toBeInTheDocument()
  })

  it('shows the last-checked time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status: 'active',
      daily_cap: 0,
      last_checked_at: '2026-07-31T14:55:00',
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/31 JUL 2026, 2:55 PM/)).toBeInTheDocument()
  })

  it('shows an Active status card when watching for mail', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status: 'active',
      daily_cap: 0,
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Active')).toBeInTheDocument()
  })

  it.each(['disabled', 'paused_cap'] as const)('shows a Paused status card for backend status %s', async (status) => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status,
      daily_cap: 0,
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Paused')).toBeInTheDocument()
  })

  it('shows a Problem status card and the error message when the mailbox check is failing', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status: 'error',
      daily_cap: 0,
      last_checked_at: null,
      last_error: "Couldn't connect to the mailbox.",
    })

    render(<EmailTriggerActivity />)

    expect(await screen.findByText('Problem')).toBeInTheDocument()
    expect(screen.getByText("Couldn't connect to the mailbox.")).toBeInTheDocument()
  })

  it('does not list individual recent runs -- that duplicate belongs on the Runs tab', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status: 'active',
      daily_cap: 0,
      last_checked_at: null,
      last_error: null,
    })

    render(<EmailTriggerActivity />)

    await screen.findByText('Active')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('clicking "View automatic runs" invokes the onViewRuns callback', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'wf-a',
      status: 'active',
      daily_cap: 0,
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
    mockedApi.getEmailTrigger.mockRejectedValue(new Error('boom'))

    render(<EmailTriggerActivity />)

    expect(await screen.findByText(/Couldn't load automatic-run status/)).toBeInTheDocument()
  })

  describe('mail the filter skipped', () => {
    const FILTERED = [
      {
        id: 7,
        external_id: '4021',
        decision: 'bulk:list-id',
        reason: 'Skipped: bulk mail (mailing list)',
        detected_at: '2026-08-16T09:30:00',
      },
      {
        id: 8,
        external_id: '4022',
        decision: 'not_allowlisted',
        reason: 'Skipped: the sender is not on your allowed list',
        detected_at: null,
      },
    ]

    beforeEach(() => {
      mockedApi.getEmailTrigger.mockResolvedValue(ACTIVE_TRIGGER)
    })

    it('lists filtered messages with a readable reason', async () => {
      mockedApi.listFilteredMessages.mockResolvedValue({ filtered: FILTERED })

      render(<EmailTriggerActivity />)

      expect(await screen.findByText('Skipped: bulk mail (mailing list)')).toBeInTheDocument()
      expect(screen.getByText('Skipped: the sender is not on your allowed list')).toBeInTheDocument()
      // `detected_at` above carries no timezone designator on purpose, so it
      // is parsed as local time and `formatDateTime` (which formats in local
      // time) renders the same wall clock everywhere. Do not "fix" it by
      // appending a Z -- that makes this assertion depend on the runner's
      // timezone.
      expect(screen.getByText(/16 AUG 2026, 9:30 AM/)).toBeInTheDocument()
      expect(screen.getByText(/4021/)).toBeInTheDocument()
    })

    it('keeps the raw decision code out of the reading line', async () => {
      // `decision` is the rule that fired -- useful when debugging a rule, but
      // it is not what an admin reads to decide whether the skip was wrong.
      mockedApi.listFilteredMessages.mockResolvedValue({ filtered: FILTERED })

      render(<EmailTriggerActivity />)

      const reason = await screen.findByText('Skipped: bulk mail (mailing list)')
      expect(reason).toHaveAttribute('title', 'bulk:list-id')
      expect(screen.queryByText('not_allowlisted')).not.toBeInTheDocument()
    })

    it('releases one and removes it from the list without a reload', async () => {
      // The same defect the Phase 3b review found in RunDetail: deleting
      // server-side and leaving the row on screen.
      mockedApi.listFilteredMessages.mockResolvedValue({ filtered: FILTERED })

      render(<EmailTriggerActivity />)

      const buttons = await screen.findAllByRole('button', { name: /release/i })
      await act(async () => {
        fireEvent.click(buttons[0])
      })

      expect(mockedApi.releaseFilteredMessage).toHaveBeenCalledWith(7)
      await waitFor(() =>
        expect(screen.queryByText('Skipped: bulk mail (mailing list)')).not.toBeInTheDocument(),
      )
      // Still there, so the removal was of that one row and not of the list.
      expect(screen.getByText('Skipped: the sender is not on your allowed list')).toBeInTheDocument()
      // No refetch: the row went because we removed it, not because the server
      // was asked again.
      expect(mockedApi.listFilteredMessages).toHaveBeenCalledTimes(1)
    })

    it('keeps a released row gone when a later poll still reports it', async () => {
      // The section re-polls every 30s, and a response prepared before the
      // release lands afterwards still carries the row. Without the guard the
      // released message silently reappears -- the very defect the test above
      // exists to prevent, merely deferred by one poll cycle.
      mockedApi.listFilteredMessages.mockResolvedValue({ filtered: FILTERED })
      vi.useFakeTimers()
      try {
        render(<EmailTriggerActivity />)
        // Flush the mount fetches without letting the interval fire.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0)
        })

        await act(async () => {
          fireEvent.click(screen.getAllByRole('button', { name: /release/i })[0])
        })
        expect(screen.queryByText('Skipped: bulk mail (mailing list)')).not.toBeInTheDocument()

        // One full poll cycle, the endpoint still returning both rows.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(30_000)
        })

        // The poll really did happen -- otherwise this test would pass for the
        // wrong reason.
        expect(mockedApi.listFilteredMessages).toHaveBeenCalledTimes(2)
        expect(screen.queryByText('Skipped: bulk mail (mailing list)')).not.toBeInTheDocument()
        // The sibling row is still there: the guard drops one id, not the list.
        expect(
          screen.getByText('Skipped: the sender is not on your allowed list'),
        ).toBeInTheDocument()
      } finally {
        vi.useRealTimers()
      }
    })

    it('keeps the row and says why when the release fails', async () => {
      mockedApi.listFilteredMessages.mockResolvedValue({ filtered: FILTERED })
      mockedApi.releaseFilteredMessage.mockRejectedValue(new Error('No such filtered message.'))

      render(<EmailTriggerActivity />)

      const buttons = await screen.findAllByRole('button', { name: /release/i })
      await act(async () => {
        fireEvent.click(buttons[0])
      })

      expect(await screen.findByText('No such filtered message.')).toBeInTheDocument()
      expect(screen.getByText('Skipped: bulk mail (mailing list)')).toBeInTheDocument()
    })

    it('says nothing was skipped rather than showing an empty list', async () => {
      render(<EmailTriggerActivity />)

      expect(await screen.findByText(/nothing has been skipped/i)).toBeInTheDocument()
    })

    it('does not claim nothing was skipped when the list could not be loaded', async () => {
      mockedApi.listFilteredMessages.mockRejectedValue(new Error('boom'))

      render(<EmailTriggerActivity />)

      expect(await screen.findByText(/Couldn't load the skipped mail/i)).toBeInTheDocument()
      expect(screen.queryByText(/nothing has been skipped/i)).not.toBeInTheDocument()
    })
  })
})

