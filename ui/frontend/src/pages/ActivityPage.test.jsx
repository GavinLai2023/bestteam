import { describe, it, expect, beforeEach, vi } from 'vitest'
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
