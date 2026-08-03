import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import RunDetail from './RunDetail'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
    listAutomationResults: vi.fn(),
    retryRun: vi.fn(),
  },
}))

class FakeWebSocket {
  constructor(url) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  close() {}
  emit(event) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}
FakeWebSocket.instances = []

describe('RunDetail', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket
    api.listAutomationResults.mockResolvedValue({ results: [] })
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('streams live events over the websocket for a running run', async () => {
    api.createWsTicket.mockResolvedValue({ ticket: 't' })

    render(<RunDetail runId="run-1" status="running" />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.createWsTicket).toHaveBeenCalled()

    const ws = FakeWebSocket.instances.at(-1)
    await act(async () => {
      ws.emit({ type: 'run_started', workflow: 'wf', agent: null, data: null, usage: [] })
    })

    expect(screen.getByText('▶ started')).toBeInTheDocument()
    expect(api.getRunTrace).not.toHaveBeenCalled()
  })

  it('refetches automation results once the live run reaches a terminal event', async () => {
    api.createWsTicket.mockResolvedValue({ ticket: 't' })
    api.listAutomationResults.mockResolvedValue({ results: [] })

    render(<RunDetail runId="run-1" status="running" />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(api.listAutomationResults).toHaveBeenCalledTimes(1))

    const ws = FakeWebSocket.instances.at(-1)
    await act(async () => {
      ws.emit({ type: 'run_completed', workflow: 'wf', agent: null, data: 'done', usage: [] })
    })

    // normalize_run_result only runs server-side after the terminal event, so
    // the initial fetch (above) can't have seen it -- this second call is what
    // actually picks up the automation results the server just wrote.
    await vi.waitFor(() => expect(api.listAutomationResults).toHaveBeenCalledTimes(2))
  })

  it('fetches the persisted trace for a finished run', async () => {
    api.getRunTrace.mockResolvedValue({
      events: [
        { seq: 0, type: 'run_started', agent: null, data: null },
        { seq: 1, type: 'run_completed', agent: null, data: 'done' },
      ],
    })

    render(<RunDetail runId="run-1" status="completed" />)

    expect(await screen.findByText('● completed')).toBeInTheDocument()
    expect(screen.getByText('Final output')).toBeInTheDocument()
    expect(api.createWsTicket).not.toHaveBeenCalled()
  })

  it('shows an error banner when the trace fetch fails', async () => {
    api.getRunTrace.mockRejectedValue(new Error('not found'))

    render(<RunDetail runId="run-1" status="failed" />)

    expect(await screen.findByText('not found')).toBeInTheDocument()
  })

  it('shows the automation results section when the run has structured results', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })
    api.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'run-1', status: 'needs_attention',
        payload: {
          priority: 'priority', summary: 'Tenant reports a leak.',
          extracted: { property_address: '12 Example St' },
          human_reason: 'Missing callback number.', action: { draft_created: true },
        },
      }],
    })

    render(<RunDetail runId="run-1" status="completed" />)

    expect(await screen.findByText('Automation results')).toBeInTheDocument()
    expect(screen.getByText('Tenant reports a leak.')).toBeInTheDocument()
    expect(screen.getByText('12 Example St')).toBeInTheDocument()
    expect(api.listAutomationResults).toHaveBeenCalledWith({ run_id: 'run-1' })
  })

  it('does not show the automation results section for a run with none', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })
    api.listAutomationResults.mockResolvedValue({ results: [] })

    render(<RunDetail runId="run-1" status="completed" />)

    await vi.waitFor(() => expect(api.listAutomationResults).toHaveBeenCalled())
    expect(screen.queryByText('Automation results')).not.toBeInTheDocument()
  })

  it('shows a Retry button for a failed run and calls the retry API on click', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })
    api.retryRun.mockResolvedValue({ run_id: 'run-2' })

    render(<RunDetail runId="run-1" status="failed" />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(api.retryRun).toHaveBeenCalledWith('run-1')
  })

  it('calls onRetried with the new run id so the caller can navigate to it', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })
    api.retryRun.mockResolvedValue({ run_id: 'run-2' })
    const onRetried = vi.fn()

    render(<RunDetail runId="run-1" status="failed" onRetried={onRetried} />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(onRetried).toHaveBeenCalledWith('run-2')
  })

  it('shows an error banner when retry fails', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })
    api.retryRun.mockRejectedValue(new Error("This run has no recorded email batch to retry."))

    render(<RunDetail runId="run-1" status="failed" />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(await screen.findByText(/no recorded email batch to retry/)).toBeInTheDocument()
  })

  it('does not show a Retry button for a completed run', async () => {
    api.getRunTrace.mockResolvedValue({ events: [] })

    render(<RunDetail runId="run-1" status="completed" />)

    await vi.waitFor(() => expect(api.listAutomationResults).toHaveBeenCalled())
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })
})
