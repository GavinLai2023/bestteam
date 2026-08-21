import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import RunDetail from './RunDetail'
import { api } from '../lib/api'
import { answerConfirm } from '../test/confirmDialog'

vi.mock('../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createWsTicket: vi.fn(),
    getRunTrace: vi.fn(),
    listAutomationResults: vi.fn(),
    retryRun: vi.fn(),
    purgeRun: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  onmessage?: (event: { data: string }) => void

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  close() {}
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

describe('RunDetail', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('streams live events over the websocket for a running run', async () => {
    mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

    render(<RunDetail runId="run-1" status="running" autonomous={false} />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(mockedApi.createWsTicket).toHaveBeenCalled()

    const ws = FakeWebSocket.instances.at(-1)
    await act(async () => {
      ws!.emit({ type: 'run_started', pipeline: 'wf', agent: null, data: null, usage: [] })
    })

    // Friendly view is the default now (F8) -- '▶ started' is the technical label.
    expect(screen.getByText('Your team got started')).toBeInTheDocument()
    expect(mockedApi.getRunTrace).not.toHaveBeenCalled()
  })

  it('refetches automation results once the live run reaches a terminal event', async () => {
    mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })

    render(<RunDetail runId="run-1" status="running" autonomous={false} />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalledTimes(1))

    const ws = FakeWebSocket.instances.at(-1)
    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'wf', agent: null, data: 'done', usage: [] })
    })

    // normalize_run_result only runs server-side after the terminal event, so
    // the initial fetch (above) can't have seen it -- this second call is what
    // actually picks up the automation results the server just wrote.
    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalledTimes(2))
  })

  it('fetches the persisted trace for a finished run', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [
        { type: 'run_started', agent: undefined, data: null },
        { type: 'run_completed', agent: undefined, data: 'done' },
      ],
    })

    render(<RunDetail runId="run-1" status="completed" autonomous={false} />)

    // Friendly view is the default now (F8) -- '● completed' is the technical label.
    expect(await screen.findByText('All done!')).toBeInTheDocument()
    expect(screen.getByText('Final output')).toBeInTheDocument()
    expect(mockedApi.createWsTicket).not.toHaveBeenCalled()
  })

  it('shows an error banner when the trace fetch fails', async () => {
    mockedApi.getRunTrace.mockRejectedValue(new Error('not found'))

    render(<RunDetail runId="run-1" status="failed" autonomous={false} />)

    expect(await screen.findByText('not found')).toBeInTheDocument()
  })

  it('shows the automation results section when the run has structured results', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'run-1', status: 'needs_attention', created_at: '2026-01-01T00:00:00Z',
        payload: {
          priority: 'priority', summary: 'Tenant reports a leak.',
          extracted: { property_address: '12 Example St' },
          human_reason: 'Missing callback number.', action: { draft_created: true },
        },
      }],
    })

    render(<RunDetail runId="run-1" status="completed" autonomous={false} />)

    expect(await screen.findByText('Automation results')).toBeInTheDocument()
    expect(screen.getByText('Tenant reports a leak.')).toBeInTheDocument()
    expect(screen.getByText('12 Example St')).toBeInTheDocument()
    expect(mockedApi.listAutomationResults).toHaveBeenCalledWith({ run_id: 'run-1' })
  })

  it('does not show the automation results section for a run with none', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })

    render(<RunDetail runId="run-1" status="completed" autonomous={false} />)

    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalled())
    expect(screen.queryByText('Automation results')).not.toBeInTheDocument()
  })

  it('shows a Retry button for a failed autonomous run and calls the retry API on click', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.retryRun.mockResolvedValue({ run_id: 'run-2' })

    render(<RunDetail runId="run-1" status="failed" autonomous />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(mockedApi.retryRun).toHaveBeenCalledWith('run-1')
  })

  it('calls onRetried with the new run id so the caller can navigate to it', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.retryRun.mockResolvedValue({ run_id: 'run-2' })
    const onRetried = vi.fn()

    render(<RunDetail runId="run-1" status="failed" autonomous onRetried={onRetried} />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(onRetried).toHaveBeenCalledWith('run-2')
  })

  it('shows an error banner when retry fails', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.retryRun.mockRejectedValue(new Error("This run has no recorded email batch to retry."))

    render(<RunDetail runId="run-1" status="failed" autonomous />)

    const button = await screen.findByText('Retry')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(await screen.findByText(/no recorded email batch to retry/)).toBeInTheDocument()
  })

  it('shows the Retry button once a live autonomous run fails, without waiting for the panel to reopen', async () => {
    // ActivityPage sets selectedRun.status at click time and never updates it
    // while the panel stays open, so `status` alone stays 'running' after a
    // live run fails mid-view -- the retry section must key off the run's own
    // terminal event too (Codex review finding).
    mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

    render(<RunDetail runId="run-1" status="running" autonomous />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()

    const ws = FakeWebSocket.instances.at(-1)
    await act(async () => {
      ws!.emit({ type: 'run_failed', pipeline: 'wf', agent: null, data: 'boom', usage: [] })
    })

    expect(await screen.findByText('Retry')).toBeInTheDocument()
  })

  it('does not show a Retry button for a completed run', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })

    render(<RunDetail runId="run-1" status="completed" autonomous />)

    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalled())
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })

  it('does not show a Retry button for a failed manual run', async () => {
    // POST /api/runs/{id}/retry only accepts a run with a recorded
    // trigger_context -- a manual run's retry attempt always 400s, so the
    // button must not even be offered (Codex review finding).
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })

    render(<RunDetail runId="run-1" status="failed" autonomous={false} />)

    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalled())
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })

  it('renders classification, category, missing information, and risk reasons', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'run-1', status: 'needs_attention', created_at: '2026-01-01T00:00:00Z',
        payload: {
          classification: 'maintenance_request', category: 'plumbing',
          priority: 'priority', summary: 'Tenant reports a leak.',
          extracted: { property_address: '12 Example St' },
          missing_information: ['callback_number', 'access_availability'],
          risk_reasons: ['active_water_leak'],
          human_reason: 'Missing callback number.', action: { draft_created: true },
        },
      }],
    })

    render(<RunDetail runId="run-1" status="completed" autonomous={false} />)

    expect(await screen.findByText('maintenance_request · plumbing')).toBeInTheDocument()
    expect(screen.getByText('Missing: callback_number, access_availability')).toBeInTheDocument()
    expect(screen.getByText('Risk: active_water_leak')).toBeInTheDocument()
  })

  // MonitorPage already narrates the same event stream in plain language by
  // default, with the raw feed one click away (audit finding F8) -- this run
  // detail view skipped that and always showed the jargon register, which is
  // what a non-technical customer sees for every run on the Activity page.
  it('narrates the trace in plain language by default, with the technical feed one click away', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [
        { type: 'run_started', agent: undefined, data: null },
        { type: 'agent_completed', agent: 'triage', data: null },
        { type: 'run_completed', agent: undefined, data: 'done' },
      ],
    })

    render(<RunDetail runId="run-1" status="completed" autonomous={false} />)

    expect(await screen.findByText('Your team got started')).toBeInTheDocument()
    expect(screen.getByText('triage finished their part')).toBeInTheDocument()
    expect(screen.queryByText('▶ started')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /show technical trace/i }))

    expect(screen.getByText('▶ started')).toBeInTheDocument()
    expect(screen.queryByText('Your team got started')).not.toBeInTheDocument()
  })

  it('says the content was removed rather than showing an empty timeline', async () => {
    // A purged run has no trace events left, which looks exactly like a bug
    // unless the panel says who removed them and what survived.
    mockedApi.getRunTrace.mockResolvedValue({
      events: [],
      usage: [],
      content_purged_at: '2026-08-17T00:00:00Z',
    })

    render(<RunDetail runId="r1" status="completed" autonomous={false} />)

    expect(await screen.findByText(/content of this run was removed/i)).toBeInTheDocument()
    expect(screen.getByText(/what it cost and when it ran are still on record/i)).toBeInTheDocument()
    expect(screen.queryByText('No trace recorded for this run.')).not.toBeInTheDocument()
  })

  it("offers to delete this run's content", async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })
    mockedApi.purgeRun.mockResolvedValue({ purged: true })

    render(<RunDetail runId="r1" status="completed" autonomous={false} />)

    const button = await screen.findByRole('button', { name: /delete this run's content/i })
    await act(async () => {
      fireEvent.click(button)
    })
    await act(async () => {
      await answerConfirm(true)
    })

    expect(mockedApi.purgeRun).toHaveBeenCalledWith('r1')
  })

  it('hides the content it just deleted, without needing a reload', async () => {
    // The events and results were fetched before the purge and are still in
    // state -- leaving them on screen contradicts the confirmation the user
    // just gave (Codex review finding).
    mockedApi.getRunTrace.mockResolvedValue({
      events: [{ type: 'run_completed', agent: undefined, data: 'Drafted a reply to alice@example.com' }],
    })
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{
        id: 1, run_id: 'r1', status: 'processed', created_at: '2026-01-01T00:00:00Z',
        payload: { summary: 'Tenant reports a leak.', action: { draft_created: true } },
      }],
    })
    mockedApi.purgeRun.mockResolvedValue({ purged: true })

    render(<RunDetail runId="r1" status="completed" autonomous={false} />)
    // The friendly view (default) doesn't repeat event data, so switch to the
    // technical trace to check both render sites clear on purge.
    fireEvent.click(await screen.findByRole('button', { name: /show technical trace/i }))
    // Twice: once in the event timeline, once as the final output.
    expect(await screen.findByText('Final output')).toBeInTheDocument()
    expect(screen.getAllByText('Drafted a reply to alice@example.com')).toHaveLength(2)
    expect(await screen.findByText('Tenant reports a leak.')).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: /delete this run's content/i }))
    })
    await act(async () => {
      await answerConfirm(true)
    })

    expect(screen.queryAllByText('Drafted a reply to alice@example.com')).toHaveLength(0)
    expect(screen.queryByText('Final output')).not.toBeInTheDocument()
    expect(screen.queryByText('Tenant reports a leak.')).not.toBeInTheDocument()
    // The result row itself stays: its status is what stops a retry
    // re-drafting the same message.
    expect(screen.getByText('Content removed.')).toBeInTheDocument()
  })

  it('does not delete the run when the confirmation is dismissed', async () => {
    mockedApi.getRunTrace.mockResolvedValue({ events: [] })

    render(<RunDetail runId="r1" status="completed" autonomous={false} />)

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: /delete this run's content/i }))
    })
    await act(async () => {
      await answerConfirm(false)
    })

    expect(mockedApi.purgeRun).not.toHaveBeenCalled()
  })

  it('does not offer to delete a run whose content is already gone', async () => {
    mockedApi.getRunTrace.mockResolvedValue({
      events: [],
      content_purged_at: '2026-08-17T00:00:00Z',
    })

    render(<RunDetail runId="r1" status="completed" autonomous={false} />)

    await screen.findByText(/content of this run was removed/i)
    expect(screen.queryByRole('button', { name: /delete this run's content/i })).not.toBeInTheDocument()
  })

  it('does not offer to delete a still-running run', async () => {
    // The worker is still writing trace events -- the API answers 409.
    mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

    render(<RunDetail runId="r1" status="running" autonomous={false} />)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.queryByRole('button', { name: /delete this run's content/i })).not.toBeInTheDocument()
  })
})
