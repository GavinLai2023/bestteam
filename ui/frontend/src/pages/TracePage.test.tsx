import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import TracePage from './TracePage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listOrgs: vi.fn(),
    listRuns: vi.fn(),
    listWorkflowAnalytics: vi.fn(),
    getWorkflowAnalytics: vi.fn(),
    getRunTrace: vi.fn(),
    createWsTicket: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const ORGS = [
  { name: 'org_a', display_name: 'Org A', active: true },
  { name: 'org_b', display_name: 'Org B', active: true },
]

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listOrgs.mockResolvedValue(ORGS)
  mockedApi.listRuns.mockResolvedValue({ runs: [], total: 0, limit: 50, offset: 0 })
  mockedApi.listWorkflowAnalytics.mockResolvedValue({ workflows: [] })
})

describe('TracePage', () => {
  it('defaults the org selector to "All organisations" and lists runs cross-org', async () => {
    render(<TracePage />)

    expect(await screen.findByDisplayValue('All organisations')).toBeInTheDocument()
    expect(mockedApi.listRuns).toHaveBeenCalledWith(
      expect.objectContaining({ org: undefined, offset: 0 }),
    )
  })

  it('switching the org selector re-fetches runs scoped to that org', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Organisation'), { target: { value: 'org_a' } })
    })

    expect(mockedApi.listRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ org: 'org_a', offset: 0 }),
    )
  })

  it('switching to the Analytics tab fetches cross-org workflow summaries by default', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')

    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    expect(mockedApi.listWorkflowAnalytics).toHaveBeenCalledWith(expect.objectContaining({ org: undefined }))
  })

  it('clicking a workflow summary row fetches its per-agent detail', async () => {
    mockedApi.listWorkflowAnalytics.mockResolvedValue({
      workflows: [
        {
          org_id: 1, org: 'org_a', workflow: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
        },
      ],
    })
    mockedApi.getWorkflowAnalytics.mockResolvedValue({
      org_id: 1, workflow: 'wf',
      per_agent: [{ agent: 'agent-a', run_count: 3, avg_input_tokens: 100, avg_output_tokens: 20, avg_cost_estimate: 0.05, avg_duration_seconds: 4 }],
      common_failure_points: [],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    const row = await screen.findByText('wf')

    await act(async () => {
      fireEvent.click(row)
    })

    expect(mockedApi.getWorkflowAnalytics).toHaveBeenCalledWith('wf', { org: 'org_a' })
    expect(await screen.findByText(/3 run\(s\)/)).toBeInTheDocument()
  })
})
