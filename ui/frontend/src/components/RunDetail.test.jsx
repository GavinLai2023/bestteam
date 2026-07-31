import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import RunDetail from './RunDetail'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
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
})
