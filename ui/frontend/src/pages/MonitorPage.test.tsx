import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import MonitorPage from './MonitorPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  API_BASE: 'http://127.0.0.1:8000',
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    listPipelines: vi.fn(),
    createRun: vi.fn(),
    createWsTicket: vi.fn(),
    cancelRun: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const renderPage = () =>
  render(
    <MemoryRouter>
      <MonitorPage />
    </MemoryRouter>,
  )

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState: number
  onopen?: () => void
  onmessage?: (event: { data: string }) => void

  constructor(url: string) {
    this.url = url
    this.readyState = 0
    FakeWebSocket.instances.push(this)
  }
  close() {
    this.readyState = 3
  }
  send(message: string) {
    FakeWebSocket.instances.at(-1)?.onopen?.()
    void message
  }
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

async function startARun() {
  mockedApi.listPipelines.mockResolvedValue({ pipelines: ['wf'] })
  mockedApi.createRun.mockResolvedValue({ run_id: 'run-1' })
  mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

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
    const err = new Error('Platform operators do not belong to an organization') as Error & { status?: number }
    err.status = 403
    mockedApi.listPipelines.mockRejectedValue(err)

    renderPage()

    expect(
      await screen.findByText(/Platform operators do not belong to an organization/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/can't reach the service/i)).not.toBeInTheDocument()
  })

  it('shows a customer-facing connection message on a genuine network failure (no HTTP status)', async () => {
    mockedApi.listPipelines.mockRejectedValue(new TypeError('Failed to fetch'))

    renderPage()

    expect(await screen.findByText(/can't reach the service/i)).toBeInTheDocument()
  })

  // The page is the customer's daily driver; which host is down and what is
  // supposed to be listening on it belongs in the console, not on screen.
  it('never shows the backend URL or the uvicorn command to the customer', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockedApi.listPipelines.mockRejectedValue(new TypeError('Failed to fetch'))

    renderPage()
    await screen.findByText(/can't reach the service/i)

    expect(screen.queryByText(/uvicorn/)).not.toBeInTheDocument()
    expect(screen.queryByText(/127\.0\.0\.1:8000/)).not.toBeInTheDocument()
    // ...but an operator can still find it.
    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('http://127.0.0.1:8000'),
      expect.anything(),
    )
    consoleError.mockRestore()
  })

  it('recovers when the retry button succeeds, without needing a reload', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockedApi.listPipelines.mockRejectedValueOnce(new TypeError('Failed to fetch'))

    renderPage()
    const banner = await screen.findByText(/can't reach the service/i)

    mockedApi.listPipelines.mockResolvedValueOnce({ pipelines: ['wf'], pipeline_ids: {} })
    await act(async () => {
      fireEvent.click(screen.getByText('Try again'))
    })

    expect(banner).not.toBeInTheDocument()
    expect(await screen.findByRole('option', { name: 'wf' })).toBeInTheDocument()
    consoleError.mockRestore()
  })
})

describe('MonitorPage run waiting UX', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('shows connecting, then connected, as the websocket opens', async () => {
    const ws = await startARun()

    expect(screen.getByText('Connecting…')).toBeInTheDocument()

    await act(async () => {
      ws!.onopen!()
    })

    expect(screen.getByText('Connected')).toBeInTheDocument()
  })

  it('shows a waiting hint until progress beyond run_queued/run_started arrives', async () => {
    const ws = await startARun()

    expect(screen.getByText('Waiting for the agent/model…')).toBeInTheDocument()

    await act(async () => {
      ws!.emit({ type: 'run_started', pipeline: 'wf', agent: null, data: null, usage: [] })
    })
    expect(screen.getByText('Waiting for the agent/model…')).toBeInTheDocument()

    await act(async () => {
      ws!.emit({ type: 'agent_started', pipeline: 'wf', agent: 'a', data: { role: 'R', goal: 'G' }, usage: [] })
    })
    expect(screen.queryByText('Waiting for the agent/model…')).not.toBeInTheDocument()
  })

  it('shows a Stop button while running that calls cancelRun', async () => {
    mockedApi.cancelRun.mockResolvedValue(undefined)
    await startARun()

    const stopButton = screen.getByText('Stop')
    await act(async () => {
      fireEvent.click(stopButton)
      await Promise.resolve()
    })

    expect(mockedApi.cancelRun).toHaveBeenCalledWith('run-1')
    expect(screen.getByText('Stopping…')).toBeInTheDocument()
  })

  it('shows a distinct "Run cancelled" result on a run_cancelled event', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'run_cancelled', pipeline: 'wf', agent: null, data: 'Run was cancelled.', usage: [] })
    })

    expect(screen.getByText('Run cancelled')).toBeInTheDocument()
    expect(screen.queryByText('Stop')).not.toBeInTheDocument()
  })

  it('does not show Stop until the new run id exists (avoids a stale-target race)', async () => {
    mockedApi.listPipelines.mockResolvedValue({ pipelines: ['wf'] })
    let resolveCreateRun: (value: { run_id: string }) => void
    mockedApi.createRun.mockReturnValue(
      new Promise((resolve) => {
        resolveCreateRun = resolve
      }),
    )
    mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

    renderPage()
    await screen.findByRole('option', { name: 'wf' })
    fireEvent.change(screen.getByLabelText('Input'), { target: { value: 'do the thing' } })
    fireEvent.click(screen.getByText('Run'))

    expect(await screen.findByText('Running…')).toBeInTheDocument()
    expect(screen.queryByText('Stop')).not.toBeInTheDocument()

    await act(async () => {
      resolveCreateRun({ run_id: 'run-1' })
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText('Stop')).toBeInTheDocument()
  })

  it('shows the result before the technical trace, collapsed by default, once the run completes', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'agent_completed', pipeline: 'wf', agent: 'a', data: 'agent step output', usage: [] })
    })
    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'the final answer', usage: [] })
    })

    expect(await screen.findByText('the final answer')).toBeInTheDocument()
    // Collapsed: the trace's own event list (which would otherwise duplicate
    // the same final text a second time) isn't rendered until expanded.
    expect(screen.queryByText('agent step output')).not.toBeInTheDocument()

    const bodyText = document.body.textContent || ''
    expect(bodyText.indexOf('Final output')).toBeLessThan(bodyText.indexOf('Live trace'))

    fireEvent.click(screen.getByText('Show technical trace'))
    expect(screen.getByText('agent step output')).toBeInTheDocument()
  })

  it('lets you copy the final result text', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'the final answer', usage: [] })
    })

    fireEvent.click(await screen.findByText('Copy'))

    expect(writeText).toHaveBeenCalledWith('the final answer')
    expect(await screen.findByText('Copied!')).toBeInTheDocument()
  })

  it('shows a failure state instead of "Copied!" when the clipboard write is rejected', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'the final answer', usage: [] })
    })

    await act(async () => {
      fireEvent.click(screen.getByText('Copy'))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.queryByText('Copied!')).not.toBeInTheDocument()
    expect(screen.getByText("Couldn't copy")).toBeInTheDocument()
  })

  it('shows a failure state instead of throwing when the Clipboard API is unavailable', async () => {
    Object.assign(navigator, { clipboard: undefined })
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'the final answer', usage: [] })
    })

    fireEvent.click(screen.getByText('Copy'))

    expect(screen.queryByText('Copied!')).not.toBeInTheDocument()
    expect(screen.getByText("Couldn't copy")).toBeInTheDocument()
  })

  it('lets you run the same team and input again from the result', async () => {
    const ws = await startARun()
    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'the final answer', usage: [] })
    })
    mockedApi.createRun.mockClear()

    await act(async () => {
      fireEvent.click(await screen.findByText('Run again'))
    })

    expect(mockedApi.createRun).toHaveBeenCalledWith('wf', 'do the thing')
  })

  it('renders object-shaped tool_completed data without crashing', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({
        type: 'tool_completed',
        pipeline: 'wf',
        agent: 'a',
        data: { tool: 'echo_tool', success: true, duration_ms: 12, summary: 'echoed: hi' },
        usage: [],
      })
    })

    expect(screen.getByText(/echo_tool · success · 12ms — echoed: hi/)).toBeInTheDocument()
  })
})
