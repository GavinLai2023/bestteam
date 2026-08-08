import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ActivityPage from './ActivityPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listWorkflows: vi.fn(),
    listRuns: vi.fn(),
    getEmailTrigger: vi.fn(),
    emailTriggerActivity: vi.fn(),
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
    automationResultsSummary: vi.fn(),
    listAutomationResults: vi.fn(),
    retryRun: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const renderPage = () =>
  render(
    <MemoryRouter>
      <ActivityPage />
    </MemoryRouter>,
  )

describe('ActivityPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listWorkflows.mockResolvedValue({ workflows: ['wf-a', 'wf-b'] })
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'off', daily_cap: 0 })
    mockedApi.emailTriggerActivity.mockResolvedValue({ runs: [] })
    mockedApi.automationResultsSummary.mockResolvedValue({
      ever_used: false,
      emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })
  })

  it('defaults to the Automations tab', async () => {
    renderPage()

    expect(await screen.findByText('Automations')).toHaveClass('active')
    expect(mockedApi.listRuns).not.toHaveBeenCalled()
  })

  it('switching to the Runs tab lists runs and lets you filter', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'wf-a' })).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledWith({})

    mockedApi.listRuns.mockResolvedValue({ runs: [] })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'wf-b' } })
    })

    expect(mockedApi.listRuns).toHaveBeenCalledWith({ workflow: 'wf-b' })
  })

  it('shows the team\'s customer-facing display name instead of the internal technical name', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [
        {
          id: 'r1',
          workflow: 'customer_support_team',
          team_display_name: 'Customer Support Team',
          status: 'completed',
          started_at: '2026-07-31T11:00:00Z',
          autonomous: false,
        },
      ],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'Customer Support Team' })).toBeInTheDocument()
    expect(screen.queryByText('customer_support_team')).not.toBeInTheDocument()
  })

  it('falls back to the internal technical name when no team display name is available', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', team_display_name: null, status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'wf-a' })).toBeInTheDocument()
  })

  it('shows a run\'s start time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T14:05:00', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByText(/31 JUL 2026, 2:05 PM/)).toBeInTheDocument()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('refreshes the run list while a run is still shown as running', async () => {
    vi.useFakeTimers()
    mockedApi.listRuns
      .mockResolvedValueOnce({
        runs: [{ id: 'r1', workflow: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      .mockResolvedValueOnce({
        runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText('running', { selector: '.status-badge' })).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(screen.getByText('completed', { selector: '.status-badge' })).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledTimes(2)
  })

  it('ignores a stale poll response that resolves after the filters changed', async () => {
    vi.useFakeTimers()
    let resolveStalePoll: (value: { runs: unknown[] }) => void
    const stalePollPromise = new Promise((resolve) => {
      resolveStalePoll = resolve
    })

    mockedApi.listRuns
      .mockResolvedValueOnce({
        runs: [{ id: 'r1', workflow: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      .mockReturnValueOnce(stalePollPromise as ReturnType<typeof api.listRuns>)
      .mockResolvedValueOnce({
        runs: [{ id: 'r2', workflow: 'wf-b', status: 'completed', started_at: '2026-07-31T11:05:00Z', autonomous: false }],
      })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByRole('heading', { name: 'wf-a' })).toBeInTheDocument()

    // The poll tick fires; its request is left pending (stalePollPromise).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    // Change filters while that poll request is still in flight.
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'wf-b' } })
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByRole('heading', { name: 'wf-b' })).toBeInTheDocument()

    // The stale poll now resolves, carrying pre-filter-change data.
    await act(async () => {
      resolveStalePoll({
        runs: [{ id: 'r1', workflow: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByRole('heading', { name: 'wf-b' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'wf-a' })).not.toBeInTheDocument()
  })

  it('"View automatic runs" on the Automations tab jumps to the Runs tab filtered to Automatic only', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      daily_cap: 0,
      last_checked_at: null,
      last_error: null,
    })
    mockedApi.listRuns.mockResolvedValue({ runs: [] })

    renderPage()
    const viewRunsButton = await screen.findByText('View automatic runs')

    await act(async () => {
      fireEvent.click(viewRunsButton)
    })

    expect(await screen.findByText('Runs')).toHaveClass('active')
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ manual: false })
  })

  it('clicking "View run" on a needs-attention item jumps to the Runs tab and opens that run', async () => {
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'run-42', status: 'needs_attention',
        created_at: '2026-08-02T10:00:00Z',
        payload: {
          priority: 'possible_emergency', summary: 'Active leak.',
          extracted: { property_address: '12 Example St' },
          human_reason: undefined, action: { draft_created: false },
        },
      }],
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.listRuns.mockResolvedValue({ runs: [] })
    Element.prototype.scrollIntoView = vi.fn()

    renderPage()
    const viewRunButton = await screen.findByText('View run')

    await act(async () => {
      fireEvent.click(viewRunButton)
    })

    expect(await screen.findByText('Runs')).toHaveClass('active')
    expect(screen.getByText('Run run-42')).toBeInTheDocument()
  })

  it('opens a needs-attention item at its real, persisted status -- not an assumed "completed" -- so Retry renders for a run that actually failed', async () => {
    // A dispatch failure still synthesizes needs_attention error rows for its
    // UIDs (see ui/backend/CLAUDE.md), so a needs-attention item's run is not
    // guaranteed to be `completed`. Hardcoding that status used to
    // permanently hide the Retry button for exactly this case (Codex review
    // finding).
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'run-42', status: 'error',
        created_at: '2026-08-02T10:00:00Z',
        payload: {
          priority: 'possible_emergency', summary: 'Dispatch failed.',
          extracted: { property_address: '12 Example St' },
          human_reason: undefined, action: { draft_created: false },
        },
      }],
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.listRuns.mockImplementation((filters) =>
      Promise.resolve({
        runs:
          filters?.run_id === 'run-42'
            ? [{ id: 'run-42', workflow: 'wf-a', status: 'failed', started_at: '2026-08-02T10:00:00Z', autonomous: true }]
            : [],
      }),
    )
    Element.prototype.scrollIntoView = vi.fn()

    renderPage()
    const viewRunButton = await screen.findByText('View run')

    await act(async () => {
      fireEvent.click(viewRunButton)
    })

    expect(await screen.findByText('Run run-42')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ run_id: 'run-42', limit: 1 })
  })

  it('scrolls the run detail panel into view when a run is selected', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })
    const runHeading = await screen.findByRole('heading', { name: 'wf-a' })

    await act(async () => {
      fireEvent.click(runHeading)
    })

    expect(scrollIntoView).toHaveBeenCalled()
  })

  it('clicking a run opens its detail via getRunTrace', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [{ type: 'run_completed', agent: undefined, data: 'done' }] })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })
    const runHeading = await screen.findByRole('heading', { name: 'wf-a' })

    await act(async () => {
      fireEvent.click(runHeading)
    })

    expect(await screen.findByText('Final output')).toBeInTheDocument()
    expect(mockedApi.getRunTrace).toHaveBeenCalledWith('r1')
  })
})
