import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ActivityPage from './ActivityPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listPipelines: vi.fn(),
    listRuns: vi.fn(),
    getActivityOverview: vi.fn(),
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
    // The page now always opens on Overview (it works the same whether or
    // not the org uses automation), so every test that needs a different
    // tab clicks its way there explicitly.
    mockedApi.getActivityOverview.mockResolvedValue({
      sessions: 0, active_days: 0, current_streak: 0, longest_streak: 0, peak_hour: null, daily_counts: [],
    })
    mockedApi.listRuns.mockResolvedValue({ runs: [] })
    mockedApi.emailTriggerActivity.mockResolvedValue({ runs: [] })
    mockedApi.automationResultsSummary.mockResolvedValue({
      ever_used: false,
      emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })
    mockedApi.listNotifications.mockResolvedValue({ notifications: [], unread: 0 })
    mockedApi.listFilteredMessages.mockResolvedValue({ filtered: [] })
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

  // Overview works the same for every org regardless of whether it uses
  // automation, which is what let this replace the old F6 Automations-vs-Runs
  // guess (an org that had never connected a mailbox used to land on an
  // Automations tab showing nothing, hiding its own runs behind a click).
  it('always opens on Overview, whether or not the org uses automation', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true, pipeline_name: 'wf-a', status: 'active', daily_cap: 50,
    })

    renderPage()

    expect(await screen.findByText('Overview')).toHaveClass('active')
    expect(screen.getByText('Automations')).not.toHaveClass('active')
    expect(screen.getByText('Runs')).not.toHaveClass('active')
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
    await act(async () => {
      fireEvent.click(screen.getByText('Automations'))
    })
    const viewRunsButton = await screen.findByText('View automatic runs')

    await act(async () => {
      fireEvent.click(viewRunsButton)
    })

    expect(await screen.findByText('Runs')).toHaveClass('active')
    expect(mockedApi.listRuns).toHaveBeenCalledWith({ manual: false, offset: 0 })
  })

  it('does not show the mail-filter or volume-limit settings here -- those live on the email team\'s Deploy page', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true, pipeline_name: 'wf-a', status: 'active', daily_cap: 0,
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Automations'))
    })
    await screen.findByText(/Automatic runs/)

    expect(screen.queryByText('Which mail to skip')).not.toBeInTheDocument()
    expect(screen.queryByText('How much automatic work to allow')).not.toBeInTheDocument()
  })

  it('clicking "View run" on a needs-attention item jumps to the Runs tab and opens that run', async () => {
    // An org with automation results necessarily has a trigger configured,
    // which is also what opens the dashboard on the Automations tab (F6).
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true, pipeline_name: 'wf-a', status: 'active', daily_cap: 50,
    })
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
    await act(async () => {
      fireEvent.click(screen.getByText('Automations'))
    })
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
    // An org with automation results necessarily has a trigger configured,
    // which is also what opens the dashboard on the Automations tab (F6).
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true, pipeline_name: 'wf-a', status: 'active', daily_cap: 50,
    })
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
    await act(async () => {
      fireEvent.click(screen.getByText('Automations'))
    })
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

  it('shows the run detail nested under the clicked run, not after the whole list', async () => {
    // Previously the panel always rendered after the pager, at the bottom of
    // the tab -- for a run near the top of a long list that put the detail
    // an entire scroll away from the row the customer just clicked.
    mockedApi.listRuns.mockResolvedValue({
      runs: [
        { id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false },
        { id: 'r2', pipeline: 'wf-b', status: 'completed', started_at: '2026-07-31T12:00:00Z', autonomous: false },
      ],
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [{ type: 'run_completed', agent: undefined, data: 'done' }] })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })
    const secondRunHeading = await screen.findByRole('heading', { name: 'wf-b' })

    await act(async () => {
      fireEvent.click(secondRunHeading)
    })

    const detail = await screen.findByText('Final output')
    expect(secondRunHeading.closest('li')).toContainElement(detail)
  })
})
