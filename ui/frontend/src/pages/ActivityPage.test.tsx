import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ActivityPage from './ActivityPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listPipelines: vi.fn(),
    listRuns: vi.fn(),
    getEmailTrigger: vi.fn(),
    emailTriggerActivity: vi.fn(),
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
    automationResultsSummary: vi.fn(),
    listAutomationResults: vi.fn(),
    retryRun: vi.fn(),
    listNotifications: vi.fn(),
    listFilteredMessages: vi.fn(),
    releaseFilteredMessage: vi.fn(),
    getEmailFilter: vi.fn(),
    setEmailFilter: vi.fn(),
    getEmailBudget: vi.fn(),
    setEmailBudget: vi.fn(),
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
    mockedApi.listPipelines.mockResolvedValue({ pipelines: ['wf-a', 'wf-b'] })
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'off', daily_cap: 0 })
    mockedApi.emailTriggerActivity.mockResolvedValue({ runs: [] })
    mockedApi.automationResultsSummary.mockResolvedValue({
      ever_used: false,
      emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })
    mockedApi.listNotifications.mockResolvedValue({ notifications: [], unread: 0 })
    mockedApi.listFilteredMessages.mockResolvedValue({ filtered: [] })
    mockedApi.getEmailFilter.mockResolvedValue({
      skip_bulk: true,
      sender_blocklist: [],
      sender_allowlist: [],
      subject_blocklist: [],
    })
    mockedApi.getEmailBudget.mockResolvedValue({
      daily_message_cap: null,
      monthly_cost_cap: null,
      messages_today: 0,
      spent_this_month: null,
      unpriced_runs_this_month: 0,
      unpriced_models: [],
    })
  })

  it('shows the unread alert badge before the Alerts tab has ever been opened', async () => {
    // The count used to arrive only through NotificationsPanel's callback,
    // and that panel is mounted only once the tab is open -- so the badge
    // could never appear before the user had already gone looking, which is
    // the one thing it exists to save them (Codex review finding).
    mockedApi.listNotifications.mockResolvedValue({ notifications: [], unread: 3 })

    renderPage()

    const alertsTab = await screen.findByRole('button', { name: /^Alerts/ })
    expect(await screen.findByText('3')).toBeInTheDocument()
    expect(alertsTab).not.toHaveClass('active')
    expect(mockedApi.listNotifications).toHaveBeenCalledWith(true, 1)
  })

  it('defaults to the Automations tab', async () => {
    renderPage()

    expect(await screen.findByText('Automations')).toHaveClass('active')
    expect(mockedApi.listRuns).not.toHaveBeenCalled()
  })

  it('switching to the Runs tab lists runs and lets you filter', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'wf-a' })).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ offset: 0 })

    mockedApi.listRuns.mockResolvedValue({ runs: [] })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'wf-b' } })
    })

    expect(mockedApi.listRuns).toHaveBeenCalledWith({ pipeline: 'wf-b', offset: 0 })
  })

  it('shows the team\'s customer-facing display name instead of the internal technical name', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [
        {
          id: 'r1',
          pipeline: 'customer_support_team',
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
      runs: [{ id: 'r1', pipeline: 'wf-a', team_display_name: null, status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'wf-a' })).toBeInTheDocument()
  })

  it('shows a run\'s start time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T14:05:00', autonomous: false }],
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
        runs: [{ id: 'r1', pipeline: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      .mockResolvedValueOnce({
        runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText('Running', { selector: '.status-badge' })).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(screen.getByText('Completed', { selector: '.status-badge' })).toBeInTheDocument()
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
        runs: [{ id: 'r1', pipeline: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      .mockReturnValueOnce(stalePollPromise as ReturnType<typeof api.listRuns>)
      .mockResolvedValueOnce({
        runs: [{ id: 'r2', pipeline: 'wf-b', status: 'completed', started_at: '2026-07-31T11:05:00Z', autonomous: false }],
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
        runs: [{ id: 'r1', pipeline: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
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
      pipeline_name: 'wf-a',
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
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ manual: false, offset: 0 })
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
    // The id is no longer the panel's title, but is still on screen for support (F7).
    expect(screen.getByText('run-42', { selector: 'code' })).toBeInTheDocument()
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
            ? [{ id: 'run-42', pipeline: 'wf-a', status: 'failed', started_at: '2026-08-02T10:00:00Z', autonomous: true }]
            : [],
      }),
    )
    Element.prototype.scrollIntoView = vi.fn()

    renderPage()
    const viewRunButton = await screen.findByText('View run')

    await act(async () => {
      fireEvent.click(viewRunButton)
    })

    // The id moved out of the heading into a support detail line (F7).
    expect(await screen.findByText('run-42', { selector: 'code' })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ run_id: 'run-42', limit: 1 })
  })

  it('scrolls the run detail panel into view when a run is selected', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
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
      runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
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

  it('offers only teams with a real id in the Shared tab picker', async () => {
    // A YAML-only demo pipeline has no `pipeline_ids` entry, so it used to
    // render with value="" -- indistinguishable from the "Pick a team…"
    // placeholder and silently doing nothing when selected. Such a pipeline
    // can't have share links at all.
    mockedApi.listPipelines.mockResolvedValue({
      pipelines: ['db-team', 'yaml-only-demo'],
      pipeline_ids: { 'db-team': 7 },
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Shared'))
    })

    expect(await screen.findByRole('option', { name: 'db-team' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'yaml-only-demo' })).not.toBeInTheDocument()
  })
})
