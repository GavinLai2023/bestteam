import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import ActivityOverviewPanel from './ActivityOverviewPanel'
import { api } from '../lib/api'
import type { ActivityOverview } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    getActivityOverview: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const OVERVIEW = (overrides: Partial<ActivityOverview> = {}): ActivityOverview => ({
  sessions: 67,
  active_days: 50,
  current_streak: 13,
  longest_streak: 16,
  peak_hour: 14,
  daily_counts: Array.from({ length: 84 }, (_, i) => ({
    date: `2026-06-${String((i % 28) + 1).padStart(2, '0')}`,
    count: i % 3,
  })),
  ...overrides,
})

describe('ActivityOverviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a loading hint before the data arrives', () => {
    mockedApi.getActivityOverview.mockReturnValue(new Promise(() => {}))

    render(<ActivityOverviewPanel />)

    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows each headline stat once loaded', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(OVERVIEW())

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('67')).toBeInTheDocument()
    expect(screen.getByText('50')).toBeInTheDocument()
    expect(screen.getByText('13')).toBeInTheDocument()
    expect(screen.getByText('16')).toBeInTheDocument()
  })

  it('formats the peak hour as 12-hour time', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(OVERVIEW({ peak_hour: 14 }))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('2 PM')).toBeInTheDocument()
  })

  it('shows a fallback instead of a broken value when there is no peak hour yet', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(OVERVIEW({ sessions: 1, peak_hour: null }))

    render(<ActivityOverviewPanel />)

    await screen.findByText('1')
    expect(screen.queryByText('null')).not.toBeInTheDocument()
    expect(screen.queryByText('NaN')).not.toBeInTheDocument()
  })

  it('shows a friendly empty state instead of an all-zero stat grid', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(
      OVERVIEW({ sessions: 0, active_days: 0, current_streak: 0, longest_streak: 0, peak_hour: null, daily_counts: [] }),
    )

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText(/no runs yet/i)).toBeInTheDocument()
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })

  it('shows an error banner when the request fails', async () => {
    mockedApi.getActivityOverview.mockRejectedValue(new Error('Service unavailable'))

    render(<ActivityOverviewPanel />)

    expect(await screen.findByText('Service unavailable')).toBeInTheDocument()
  })

  it('renders one heatmap cell per day of data', async () => {
    mockedApi.getActivityOverview.mockResolvedValue(OVERVIEW())

    const { container } = render(<ActivityOverviewPanel />)
    await screen.findByText('67')

    expect(container.querySelectorAll('.overview-heatmap-cell')).toHaveLength(84)
  })
})
