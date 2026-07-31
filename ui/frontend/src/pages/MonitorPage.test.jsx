import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import MonitorPage from './MonitorPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  API_BASE: 'http://127.0.0.1:8000',
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    listWorkflows: vi.fn(),
    createRun: vi.fn(),
    createWsTicket: vi.fn(),
    cancelRun: vi.fn(),
  },
}))

const renderPage = () =>
  render(
    <MemoryRouter>
      <MonitorPage />
    </MemoryRouter>,
  )

class FakeWebSocket {
  constructor(url) {
    this.url = url
    this.readyState = 0
    FakeWebSocket.instances.push(this)
  }
  close() {
    this.readyState = 3
  }
  send(message) {
    FakeWebSocket.instances.at(-1)?.onopen?.()
    void message
  }
  emit(event) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}
FakeWebSocket.instances = []

async function startARun() {
  api.listWorkflows.mockResolvedValue({ workflows: ['wf'] })
  api.createRun.mockResolvedValue({ run_id: 'run-1' })
  api.createWsTicket.mockResolvedValue({ ticket: 't' })

  renderPage()
  await screen.findByRole('option', { name: 'wf' })
  fireEvent.change(screen.getByLabelText('Input'), { target: { value: 'do the thing' } })
  await act(async () => {
    fireEvent.click(screen.getByText('Run'))
    // Flush the createRun/createWsTicket awaits so the WebSocket is constructed.
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
  return FakeWebSocket.instances.at(-1)
}

describe('MonitorPage backend error handling', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the server error detail, not "unreachable", when the backend returns an HTTP error', async () => {
    const err = new Error('Platform operators do not belong to an organization')
    err.status = 403
    api.listWorkflows.mockRejectedValue(err)

    renderPage()

    expect(
      await screen.findByText(/Platform operators do not belong to an organization/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Can't reach the backend/)).not.toBeInTheDocument()
  })

  it('shows "Can\'t reach the backend" on a genuine network failure (no HTTP status)', async () => {
    api.listWorkflows.mockRejectedValue(new TypeError('Failed to fetch'))

    renderPage()

    expect(await screen.findByText(/Can't reach the backend/)).toBeInTheDocument()
  })
})

describe('MonitorPage run waiting UX', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('shows connecting, then connected, as the websocket opens', async () => {
    const ws = await startARun()

    expect(screen.getByText('Connecting…')).toBeInTheDocument()

    await act(async () => {
      ws.onopen()
    })

    expect(screen.getByText('Connected')).toBeInTheDocument()
  })

  it('shows a waiting hint until progress beyond run_queued/run_started arrives', async () => {
    const ws = await startARun()

    expect(screen.getByText('Waiting for the agent/model…')).toBeInTheDocument()

    await act(async () => {
      ws.emit({ type: 'run_started', workflow: 'wf', agent: null, data: null, usage: [] })
    })
    expect(screen.getByText('Waiting for the agent/model…')).toBeInTheDocument()

    await act(async () => {
      ws.emit({ type: 'agent_started', workflow: 'wf', agent: 'a', data: { role: 'R', goal: 'G' }, usage: [] })
    })
    expect(screen.queryByText('Waiting for the agent/model…')).not.toBeInTheDocument()
  })

  it('shows a Stop button while running that calls cancelRun', async () => {
    api.cancelRun.mockResolvedValue({ status: 'cancel_requested' })
    await startARun()

    const stopButton = screen.getByText('Stop')
    await act(async () => {
      fireEvent.click(stopButton)
      await Promise.resolve()
    })

    expect(api.cancelRun).toHaveBeenCalledWith('run-1')
    expect(screen.getByText('Stopping…')).toBeInTheDocument()
  })

  it('shows a distinct "Run cancelled" result on a run_cancelled event', async () => {
    const ws = await startARun()

    await act(async () => {
      ws.emit({ type: 'run_cancelled', workflow: 'wf', agent: null, data: 'Run was cancelled.', usage: [] })
    })

    expect(screen.getByText('Run cancelled')).toBeInTheDocument()
    expect(screen.queryByText('Stop')).not.toBeInTheDocument()
  })

  it('renders object-shaped tool_completed data without crashing', async () => {
    const ws = await startARun()

    await act(async () => {
      ws.emit({
        type: 'tool_completed',
        workflow: 'wf',
        agent: 'a',
        data: { tool: 'echo_tool', success: true, duration_ms: 12, summary: 'echoed: hi' },
        usage: [],
      })
    })

    expect(screen.getByText(/echo_tool · success · 12ms — echoed: hi/)).toBeInTheDocument()
  })
})
