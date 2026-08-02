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
  },
}))

const renderPage = () =>
  render(
    <MemoryRouter>
      <ActivityPage />
    </MemoryRouter>,
  )

describe('ActivityPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listWorkflows.mockResolvedValue({ workflows: ['wf-a', 'wf-b'] })
    api.getEmailTrigger.mockResolvedValue({ enabled: false })
    api.emailTriggerActivity.mockResolvedValue({ runs: [] })
  })

  it('defaults to the Automations tab', async () => {
    renderPage()

    expect(await screen.findByText('Automations')).toHaveClass('active')
    expect(api.listRuns).not.toHaveBeenCalled()
  })

  it('switching to the Runs tab lists runs and lets you filter', async () => {
    api.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })

    expect(await screen.findByRole('heading', { name: 'wf-a' })).toBeInTheDocument()
    expect(api.listRuns).toHaveBeenCalledWith({})

    api.listRuns.mockResolvedValue({ runs: [] })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'wf-b' } })
    })

    expect(api.listRuns).toHaveBeenCalledWith({ workflow: 'wf-b' })
  })

  it('shows a run\'s start time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    api.listRuns.mockResolvedValue({
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
    api.listRuns
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
    expect(api.listRuns).toHaveBeenCalledTimes(2)
  })

  it('ignores a stale poll response that resolves after the filters changed', async () => {
    vi.useFakeTimers()
    let resolveStalePoll
    const stalePollPromise = new Promise((resolve) => {
      resolveStalePoll = resolve
    })

    api.listRuns
      .mockResolvedValueOnce({
        runs: [{ id: 'r1', workflow: 'wf-a', status: 'running', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      })
      .mockReturnValueOnce(stalePollPromise)
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
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'wf-a',
      status: 'active',
      last_checked_at: null,
      last_error: null,
    })
    api.listRuns.mockResolvedValue({ runs: [] })

    renderPage()
    const viewRunsButton = await screen.findByText('View automatic runs')

    await act(async () => {
      fireEvent.click(viewRunsButton)
    })

    expect(await screen.findByText('Runs')).toHaveClass('active')
    expect(api.listRuns).toHaveBeenCalledWith({ manual: false })
  })

  it('scrolls the run detail panel into view when a run is selected', async () => {
    api.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })
    api.getRunTrace.mockResolvedValue({ events: [] })
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
    api.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', workflow: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
    })
    api.getRunTrace.mockResolvedValue({ events: [{ seq: 0, type: 'run_completed', agent: null, data: 'done' }] })

    renderPage()
    await act(async () => {
      fireEvent.click(screen.getByText('Runs'))
    })
    const runHeading = await screen.findByRole('heading', { name: 'wf-a' })

    await act(async () => {
      fireEvent.click(runHeading)
    })

    expect(await screen.findByText('Final output')).toBeInTheDocument()
    expect(api.getRunTrace).toHaveBeenCalledWith('r1')
  })
})
