import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import DataRetentionPanel from './DataRetentionPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getRetention: vi.fn(),
    setRetention: vi.fn(),
    purgeRuns: vi.fn(),
    exportOrgData: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const settings = (over = {}) => ({
  run_retention_days: null,
  last_swept_at: null,
  last_purged_count: 0,
  purgeable_now: 0,
  ...over,
})

describe('DataRetentionPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getRetention.mockResolvedValue(settings())
    mockedApi.setRetention.mockResolvedValue(settings({ run_retention_days: 30 }))
    mockedApi.purgeRuns.mockResolvedValue({ purged: 0 })
    mockedApi.exportOrgData.mockResolvedValue({
      org_id: 1,
      exported_at: '2026-08-17T00:00:00+00:00',
      truncated: false,
      oldest_included: null,
      runs: [],
    })
  })

  it('shows the policy as off by default', async () => {
    render(<DataRetentionPanel />)
    expect(await screen.findByText(/kept forever/i)).toBeInTheDocument()
  })

  it('says plainly what a cleanup removes and what it keeps', async () => {
    // This is the screen where somebody decides to delete their own data, so
    // the promise has to match what the backend actually does: content goes,
    // accounting stays (see ui/backend/retention.py).
    render(<DataRetentionPanel />)
    expect(
      await screen.findByText(/the message text, the reply we drafted and the step-by-step trace/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/we keep that the run happened, when, and what it cost/i)).toBeInTheDocument()
  })

  it('saves a retention period', async () => {
    render(<DataRetentionPanel />)
    fireEvent.change(await screen.findByLabelText(/how long to keep/i), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(mockedApi.setRetention).toHaveBeenCalledWith(30))
  })

  it('warns how many runs the saved period will remove', async () => {
    mockedApi.getRetention.mockResolvedValue(settings({ run_retention_days: 30, purgeable_now: 4 }))
    render(<DataRetentionPanel />)
    expect(await screen.findByText(/remove the content of 4 past runs/i)).toBeInTheDocument()
  })

  it('requires typing DELETE before purging', async () => {
    mockedApi.getRetention.mockResolvedValue(settings({ run_retention_days: 30 }))
    render(<DataRetentionPanel />)

    const button = await screen.findByRole('button', { name: /delete now/i })
    expect(button).toBeDisabled()
    fireEvent.click(button)

    expect(mockedApi.purgeRuns).not.toHaveBeenCalled()
  })

  it('purges after the confirmation is typed', async () => {
    mockedApi.getRetention.mockResolvedValue(settings({ run_retention_days: 30 }))
    mockedApi.purgeRuns.mockResolvedValue({ purged: 2 })
    render(<DataRetentionPanel />)

    fireEvent.change(await screen.findByLabelText(/type delete to confirm/i), {
      target: { value: 'DELETE' },
    })
    fireEvent.click(screen.getByRole('button', { name: /delete now/i }))

    await waitFor(() => expect(mockedApi.purgeRuns).toHaveBeenCalledWith(30))
  })

  it('cannot delete now while the period is Keep forever', async () => {
    // "Delete now" uses the selected period as its window, and keep-forever
    // is not a window -- the API requires an explicit one.
    render(<DataRetentionPanel />)

    fireEvent.change(await screen.findByLabelText(/type delete to confirm/i), {
      target: { value: 'DELETE' },
    })
    expect(screen.getByRole('button', { name: /delete now/i })).toBeDisabled()
  })

  it('shows when the last cleanup ran', async () => {
    mockedApi.getRetention.mockResolvedValue(
      settings({
        run_retention_days: 30,
        last_swept_at: '2026-08-17T09:00:00+00:00',
        last_purged_count: 7,
      }),
    )
    render(<DataRetentionPanel />)
    expect(await screen.findByText(/last cleanup:.*removed 7 runs/i)).toBeInTheDocument()
  })

  it('surfaces an export failure', async () => {
    mockedApi.exportOrgData.mockRejectedValue(new Error('Export failed.'))
    render(<DataRetentionPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /download export/i }))

    expect(await screen.findByText(/export failed/i)).toBeInTheDocument()
  })
})
