import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import PreviewPage from './PreviewPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  WS_BASE: 'ws://127.0.0.1:8000',
  api: {
    createTestRun: vi.fn(),
    createWsTicket: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

let mockContext: {
  session: BuilderSession | null
  setSession: (session: BuilderSession) => void
  loading: boolean
  sessionId: string
}

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useOutletContext: () => mockContext, useNavigate: () => vi.fn() }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <PreviewPage />
    </MemoryRouter>,
  )

const sessionWithSpec = (): BuilderSession => ({
  id: 's1',
  status: 'spec',
  intent_text: 'answer support questions',
  specification_json: { name: 'support_team', agents: [], teams: [] },
  uses_email: false,
  updated_at: '2026-08-21T00:00:00Z',
})

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState: number
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  onclose?: () => void

  constructor(url: string) {
    this.url = url
    this.readyState = 0
    FakeWebSocket.instances.push(this)
  }
  close() {
    this.readyState = 3
  }
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

async function startARun() {
  mockedApi.createTestRun.mockResolvedValue({ run_id: 'run-1' })
  mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })

  renderPage()
  fireEvent.change(screen.getByLabelText('A real task or message for your team'), {
    target: { value: 'a real task' },
  })
  await act(async () => {
    fireEvent.click(screen.getByText('Run this through your team'))
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
  return FakeWebSocket.instances.at(-1)
}

describe('PreviewPage Continue button while a test run is in flight', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1' }
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('is enabled before any run has been tried', () => {
    renderPage()

    expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled()
  })

  it('disables Continue while the run is in progress', async () => {
    await startARun()

    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
  })

  it('re-enables Continue once the run completes', async () => {
    const ws = await startARun()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()

    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'support_team', agent: null, data: 'done', usage: [] })
    })

    expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled()
  })

  it('re-enables Continue if the run fails', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'run_failed', pipeline: 'support_team', agent: null, data: 'boom', usage: [] })
    })

    expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled()
  })
})
