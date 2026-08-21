import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import AdminRunDetail from './AdminRunDetail'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
    diagnoseRun: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('AdminRunDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('groups events by agent, unlike the customer RunDetail\'s single flat list', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [
        { type: 'run_started', agent: undefined, data: null },
        { type: 'agent_started', agent: 'agent-a', data: { role: 'R', goal: 'G' } },
        { type: 'agent_completed', agent: 'agent-a', data: 'done' },
      ],
      usage: [],
    })

    render(<AdminRunDetail runId="run-1" status="completed" />)

    expect(await screen.findByRole('heading', { level: 3, name: 'agent-a' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Run' })).toBeInTheDocument()
  })

  it('renders per-agent token/cost usage', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [{ type: 'agent_completed', agent: 'agent-a', data: 'done' }],
      usage: [{ agent: 'agent-a', model: 'fake:x', input_tokens: 10, output_tokens: 5, cost_estimate: 0.01 }],
    })

    render(<AdminRunDetail runId="run-1" status="completed" />)

    expect(await screen.findByText('fake:x')).toBeInTheDocument()
    expect(screen.getByText(/10 in \/ 5 out tokens/)).toBeInTheDocument()
    expect(screen.getByText(/\$0\.0100/)).toBeInTheDocument()
  })

  it('renders the full raw event data alongside the friendly summary', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [
        {
          type: 'tool_completed',
          agent: 'agent-a',
          data: { tool: 'web_search', success: true, duration_ms: 120, summary: 'ok' },
        },
      ],
      usage: [],
    })

    render(<AdminRunDetail runId="run-1" status="completed" />)

    expect(await screen.findByText(/web_search · success · 120ms — ok/)).toBeInTheDocument()
    expect(screen.getByText(/"tool": "web_search"/)).toBeInTheDocument()
  })

  it('shows a hint when there is no trace yet', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [], usage: [] })

    render(<AdminRunDetail runId="run-1" status="completed" />)

    expect(await screen.findByText('No trace recorded for this run.')).toBeInTheDocument()
  })

  // --- Diagnostic re-run (docs/superpowers/specs/2026-08-21-diagnostic-rerun-design.md) ---

  it('offers "Diagnose this run" on a finished run and reports the new run', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [], usage: [] })
    mockedApi.diagnoseRun.mockResolvedValue({ run_id: 'run-2', diagnostic_of_run_id: 'run-1', version_changed: true })
    const onDiagnosed = vi.fn()

    render(<AdminRunDetail runId="run-1" status="completed" onDiagnosed={onDiagnosed} />)

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Diagnose this run' }))
    })

    expect(mockedApi.diagnoseRun).toHaveBeenCalledWith('run-1')
    expect(onDiagnosed).toHaveBeenCalledWith({ run_id: 'run-2', diagnostic_of_run_id: 'run-1', version_changed: true })
  })

  it('shows the refusal reason when the backend declines to diagnose', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [], usage: [] })
    mockedApi.diagnoseRun.mockRejectedValue(new Error('Autonomous email runs and shared-chat turns can\'t be diagnosed'))

    render(<AdminRunDetail runId="run-1" status="completed" onDiagnosed={vi.fn()} />)
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Diagnose this run' }))
    })

    expect(await screen.findByText(/can't be diagnosed/)).toBeInTheDocument()
  })

  it('does not offer to diagnose a running run', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [], usage: [] })

    await act(async () => {
      render(<AdminRunDetail runId="run-1" status="running" onDiagnosed={vi.fn()} />)
    })

    expect(screen.queryByRole('button', { name: 'Diagnose this run' })).not.toBeInTheDocument()
  })

  it('labels a diagnostic run, links back to the original and never offers to diagnose it again', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [], usage: [] })
    const onOpenRun = vi.fn()

    render(
      <AdminRunDetail
        runId="run-2"
        status="completed"
        diagnosticOfRunId="run-1"
        versionChanged
        onDiagnosed={vi.fn()}
        onOpenRun={onOpenRun}
      />,
    )

    expect(await screen.findByText(/Diagnostic re-run of run run-1/)).toBeInTheDocument()
    expect(screen.getByText(/Memory context is not reproduced/)).toBeInTheDocument()
    expect(screen.getByText(/redeployed after the original run/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Diagnose this run' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open original run' }))
    expect(onOpenRun).toHaveBeenCalledWith('run-1')
  })

  it('collapses the long diagnostic payloads behind a details toggle', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [
        { type: 'agent_prompt', agent: 'a', data: { system_prompt: 'You are a.', input: 'hi' } },
        { type: 'model_turn', agent: 'a', data: { turn: 1, content: 'done', tool_calls: [] } },
        {
          type: 'tool_completed',
          agent: 'a',
          data: { tool: 'docs', success: true, duration_ms: 5, summary: '1 result', result: 'full excerpt text' },
        },
        { type: 'agent_started', agent: 'a', data: { role: 'R', goal: 'G' } },
      ],
      usage: [],
    })

    render(<AdminRunDetail runId="run-2" status="completed" diagnosticOfRunId="run-1" />)

    await screen.findByText('system prompt 10 chars · input 2 chars')
    // The three diagnostic payloads are in a <details>; the ordinary
    // agent_started raw JSON stays inline as before.
    const details = document.querySelectorAll('details.admin-run-detail-raw-details')
    expect(details).toHaveLength(3)
    expect(screen.getByText(/"system_prompt": "You are a\."/)).toBeInTheDocument()
    expect(screen.getByText(/"result": "full excerpt text"/)).toBeInTheDocument()
    expect(screen.getByText(/"role": "R"/).closest('details')).toBeNull()
  })
})
