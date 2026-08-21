import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ActivityOverviewPanel from './ActivityOverviewPanel'
import { api } from '../lib/api'
import type { ActivityOverview } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    getActivityOverview: vi.fn(),
    listPipelines: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const overview = (overrides: Partial<ActivityOverview> = {}): ActivityOverview => ({
  sessions: 7,
  active_days: 3,
  current_streak: 2,
  longest_streak: 2,
  peak_hour: 14,
  daily_counts: [{ date: '2026-08-20', count: 3 }],
  completed_count: 5,
  team_counts: [],
  ...overrides,
})

describe('ActivityOverviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listPipelines.mockResolvedValue({ pipelines: [] })
  })

  it('shows a loading hint before the data arrives', () => {
    mockedApi.getActivityOverview.mockReturnValue(new Promise(() => {}))

    render(<ActivityOverviewPanel />)

    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows the completed-task count as the headline, not a bare "Sessions" count', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(overview({ completed_count: 5 }))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('5')).toBeInTheDocument()
    expect(screen.getByText('Tasks completed')).toBeInTheDocument()
    expect(screen.queryByText('Sessions')).not.toBeInTheDocument()
  })

  it('does not show a busiest-hour card', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(overview())

    render(<ActivityOverviewPanel />)

    await screen.findByText('Tasks completed')
    expect(screen.queryByText('Busiest hour')).not.toBeInTheDocument()
  })

  it("breaks completed work down by each team's friendly display name", async () => {
    mockedApi.getActivityOverview.mockResolvedValue(
      overview({
        team_counts: [
          { pipeline: 'payroll_qa', count: 5 },
          { pipeline: 'sales_bot', count: 2 },
        ],
      }),
    )
    mockedApi.listPipelines.mockResolvedValue({
      pipelines: ['payroll_qa', 'sales_bot'],
      display_names: { payroll_qa: 'Payroll Q&A Team' },
    })

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('Payroll Q&A Team')).toBeInTheDocument()
    expect(screen.getByText('5 tasks')).toBeInTheDocument()
    // No friendly name on record for sales_bot -- falls back to the raw name
    // rather than hiding the row.
    expect(screen.getByText('sales_bot')).toBeInTheDocument()
    expect(screen.getByText('2 tasks')).toBeInTheDocument()
  })

  it('shows the longest-streak note only when there is a personal best to report', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(overview({ current_streak: 2, longest_streak: 5 }))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('Personal best: 5 days')).toBeInTheDocument()
  })

  it('omits the streak note entirely when there is no streak yet', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(overview({ current_streak: 0, longest_streak: 0 }))

    render(<ActivityOverviewPanel />)

    await screen.findByText('Tasks completed')
    expect(screen.queryByText(/Personal best/)).not.toBeInTheDocument()
  })

  it('still shows the empty state for an org with no runs at all', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(overview({ sessions: 0, completed_count: 0 }))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument()
    expect(screen.queryByText('Tasks completed')).not.toBeInTheDocument()
  })

  it('shows a friendly banner when the request fails, not the raw error text', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockedApi.getActivityOverview.mockRejectedValue(new Error('{"detail": "Not Found"}'))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText(/couldn't load your activity/i)).toBeInTheDocument()
    expect(screen.queryByText('{"detail": "Not Found"}')).not.toBeInTheDocument()
    expect(consoleError).toHaveBeenCalled()
    consoleError.mockRestore()
  })

  it('retries the fetch when Try again is clicked after a failure', async () => {
    mockedApi.getActivityOverview.mockRejectedValueOnce(new Error('Not Found'))
    mockedApi.getActivityOverview.mockResolvedValueOnce(overview({ completed_count: 1 }))

    render(<ActivityOverviewPanel />)

    const retryButton = await screen.findByRole('button', { name: /try again/i })
    fireEvent.click(retryButton)

    expect(await screen.findByText('Tasks completed')).toBeInTheDocument()
    expect(mockedApi.getActivityOverview).toHaveBeenCalledTimes(2)
  })

  it('renders one heatmap cell per day of data', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(
      overview({ daily_counts: Array.from({ length: 84 }, (_, i) => ({ date: `2026-06-${String((i % 28) + 1).padStart(2, '0')}`, count: i % 3 })) }),
    )

    const { container } = render(<ActivityOverviewPanel />)
    await screen.findByText('Tasks completed')

    expect(container.querySelectorAll('.overview-heatmap-cell')).toHaveLength(84)
  })
})
