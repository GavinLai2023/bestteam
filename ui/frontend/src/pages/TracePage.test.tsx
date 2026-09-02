import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import TracePage from './TracePage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listOrgs: vi.fn(),
    listRuns: vi.fn(),
    listPipelineAnalytics: vi.fn(),
    listModelAnalytics: vi.fn(),
    getPipelineAnalytics: vi.fn(),
    getRunTrace: vi.fn(),
    createWsTicket: vi.fn(),
    diagnoseRun: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const ORGS = [
  { name: 'org_a', display_name: 'Org A', active: true },
  { name: 'org_b', display_name: 'Org B', active: true },
]

const NO_DRAFTS = {
  sent: 0, handled: 0, pending: 0, unknown: 0,
  by_evidence: { source_key_header: 0, in_reply_to: 0 },
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listOrgs.mockResolvedValue(ORGS)
  mockedApi.listRuns.mockResolvedValue({ runs: [], total: 0, limit: 50, offset: 0 })
  mockedApi.listPipelineAnalytics.mockResolvedValue({ pipelines: [] })
  mockedApi.listModelAnalytics.mockResolvedValue({ models: [] })
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
    // The select's options come from the async listOrgs -- wait for them, or a
    // change to 'org_a' on a slow runner is a no-op on an option-less select.
    await screen.findByRole('option', { name: 'Org A' })

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Organisation'), { target: { value: 'org_a' } })
    })

    expect(mockedApi.listRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ org: 'org_a', offset: 0 }),
    )
  })

  it('scrolls the run detail panel into view when a run is selected, like the customer-facing Activity page', async () => {
    // The panel used to render after the whole runs list + pager, off-screen
    // for any run not right at the top -- ActivityPage.tsx already solved
    // this the same way for the customer-facing Runs tab.
    mockedApi.listRuns.mockResolvedValue({
      runs: [{ id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false }],
      total: 1, limit: 50, offset: 0,
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView

    render(<TracePage />)
    const runHeading = await screen.findByRole('heading', { name: 'wf-a' })

    await act(async () => {
      fireEvent.click(runHeading)
    })

    expect(scrollIntoView).toHaveBeenCalled()
  })

  it('scrolls the pipeline detail panel into view when a pipeline row is selected', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })
    mockedApi.getPipelineAnalytics.mockResolvedValue({
      org_id: 1, pipeline: 'wf', per_agent: [], per_model: [], common_failure_points: [],
      draft_outcomes: NO_DRAFTS,
    })
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    const row = await screen.findByText('wf')

    await act(async () => {
      fireEvent.click(row)
    })

    expect(scrollIntoView).toHaveBeenCalled()
  })

  it('switching to the Analytics tab fetches cross-org pipeline summaries by default', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')

    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    expect(mockedApi.listPipelineAnalytics).toHaveBeenCalledWith(expect.objectContaining({ org: undefined }))
  })

  it('clicking a pipeline summary row fetches its per-agent detail', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })
    mockedApi.getPipelineAnalytics.mockResolvedValue({
      org_id: 1, pipeline: 'wf',
      per_agent: [{ agent: 'agent-a', run_count: 3, avg_input_tokens: 100, avg_output_tokens: 20, avg_cost_estimate: 0.05, avg_duration_seconds: 4 }],
      per_model: [],
      common_failure_points: [],
      draft_outcomes: NO_DRAFTS,
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    const row = await screen.findByText('wf')

    await act(async () => {
      fireEvent.click(row)
    })

    expect(mockedApi.getPipelineAnalytics).toHaveBeenCalledWith('wf', { org: 'org_a' })
    expect(await screen.findByText(/3 run\(s\)/)).toBeInTheDocument()
  })

  it('renders a per-model breakdown in the pipeline detail panel', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 300, total_output_tokens: 60, total_cost_estimate: 0.4,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })
    mockedApi.getPipelineAnalytics.mockResolvedValue({
      org_id: 1, pipeline: 'wf',
      per_agent: [],
      per_model: [{ model: 'openai:gpt-4o-mini', run_count: 3, avg_input_tokens: 100, avg_output_tokens: 20, avg_cost_estimate: 0.05 }],
      common_failure_points: [],
      draft_outcomes: NO_DRAFTS,
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    const row = await screen.findByText('wf')
    await act(async () => {
      fireEvent.click(row)
    })

    expect(await screen.findByText('openai:gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText(/100 in \/ 20 out tokens avg per call/)).toBeInTheDocument()
  })

  it('renders token and cost totals in the summary table', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 98497, total_output_tokens: 3928, total_cost_estimate: 0.0171,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    expect(await screen.findByText('98,497')).toBeInTheDocument()
    expect(screen.getByText('3,928')).toBeInTheDocument()
    expect(screen.getByText('$0.0171')).toBeInTheDocument()
  })

  it('renders a dash for a pipeline with no cost data', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 1, completed: 1, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: null,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    await screen.findByText('wf')
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('renders the draft outcome breakdown in the summary table', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 3, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: null,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: {
            sent: 5, handled: 2, pending: 3, unknown: 1,
            by_evidence: { source_key_header: 4, in_reply_to: 1 },
          },
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    expect(await screen.findByText('5 / 2 / 3')).toBeInTheDocument()
  })

  it('renders a dash in the drafts column for a pipeline that wrote none', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 1, completed: 1, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: 3,
          total_input_tokens: 10, total_output_tokens: 5, total_cost_estimate: 0.5,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    await screen.findByText('wf')
    expect(screen.queryByText(/0 \/ 0 \/ 0/)).not.toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('renders the draft outcome detail with its evidence split', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 3, completed: 3, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: null,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: {
            sent: 5, handled: 2, pending: 3, unknown: 1,
            by_evidence: { source_key_header: 4, in_reply_to: 1 },
          },
        },
      ],
    })
    mockedApi.getPipelineAnalytics.mockResolvedValue({
      org_id: 1, pipeline: 'wf', per_agent: [], per_model: [], common_failure_points: [],
      draft_outcomes: {
        sent: 5, handled: 2, pending: 3, unknown: 1,
        by_evidence: { source_key_header: 4, in_reply_to: 1 },
      },
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('wf'))
    })

    expect(await screen.findByText('Draft outcomes')).toBeInTheDocument()
    expect(screen.getByText(/4 by our own header/)).toBeInTheDocument()
    expect(screen.getByText(/1 by reply threading/)).toBeInTheDocument()
  })

  it('says so plainly when the selected pipeline wrote no drafts', async () => {
    mockedApi.listPipelineAnalytics.mockResolvedValue({
      pipelines: [
        {
          org_id: 1, org: 'org_a', pipeline: 'wf', total_runs: 1, completed: 1, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: null,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
          draft_outcomes: NO_DRAFTS,
        },
      ],
    })
    mockedApi.getPipelineAnalytics.mockResolvedValue({
      org_id: 1, pipeline: 'wf', per_agent: [], per_model: [], common_failure_points: [],
      draft_outcomes: NO_DRAFTS,
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('wf'))
    })

    expect(await screen.findByText(/no drafts written by this pipeline/i)).toBeInTheDocument()
  })

  it('switching to the By model tab fetches cross-org model totals by default', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')

    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    expect(mockedApi.listModelAnalytics).toHaveBeenCalledWith(expect.objectContaining({ org: undefined }))
  })

  it('renders model rows with token and cost totals', async () => {
    mockedApi.listModelAnalytics.mockResolvedValue({
      models: [
        { model: 'openai:gpt-4o-mini', run_count: 13, total_input_tokens: 98497, total_output_tokens: 3928, total_cost_estimate: 0.0171 },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    expect(await screen.findByText('openai:gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText('98,497')).toBeInTheDocument()
    expect(screen.getByText('$0.0171')).toBeInTheDocument()
  })

  it('switching the org selector on the By model tab re-fetches scoped to that org', async () => {
    render(<TracePage />)
    // The select's options come from the async listOrgs -- wait for them, or a
    // change to 'org_a' on a slow runner is a no-op on an option-less select.
    await screen.findByRole('option', { name: 'Org A' })
    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Organisation'), { target: { value: 'org_a' } })
    })

    expect(mockedApi.listModelAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ org: 'org_a' }))
  })

  it('badges diagnostic runs in the list and opens the new run after "Diagnose this run"', async () => {
    mockedApi.listRuns.mockResolvedValue({
      runs: [
        { id: 'r1', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T11:00:00Z', autonomous: false, diagnostic_of_run_id: null },
        { id: 'r0', pipeline: 'wf-a', status: 'completed', started_at: '2026-07-31T10:00:00Z', autonomous: false, diagnostic_of_run_id: 'r-old', version_changed: true },
      ],
      total: 2, limit: 50, offset: 0,
    })
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.diagnoseRun.mockResolvedValue({ run_id: 'r2', diagnostic_of_run_id: 'r1', version_changed: false })
    Element.prototype.scrollIntoView = vi.fn()

    render(<TracePage />)
    expect(await screen.findByText('diagnostic')).toBeInTheDocument()

    // Reselecting a diagnostic run from the list restores the redeployment
    // warning from the row's derived `version_changed`, not just from the
    // POST response that created it.
    const [, oldRunHeading] = await screen.findAllByRole('heading', { name: 'wf-a' })
    await act(async () => {
      fireEvent.click(oldRunHeading)
    })
    expect(await screen.findByText(/Diagnostic re-run of run r-old/)).toBeInTheDocument()
    expect(screen.getByText(/redeployed after the original run/)).toBeInTheDocument()

    const [runHeading] = await screen.findAllByRole('heading', { name: 'wf-a' })
    await act(async () => {
      fireEvent.click(runHeading)
    })
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Diagnose this run' }))
    })

    expect(mockedApi.diagnoseRun).toHaveBeenCalledWith('r1')
    expect(await screen.findByRole('heading', { name: 'Run r2' })).toBeInTheDocument()
    expect(screen.getByText(/Diagnostic re-run of run r1/)).toBeInTheDocument()
  })
})
