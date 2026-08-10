import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import AdminRunDetail from './AdminRunDetail'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
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
})
