import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ShareChatPage from './ShareChatPage'
import { shareChatApi } from '../lib/shareChatApi'

vi.mock('../lib/shareChatApi', () => ({
  shareChatApi: {
    getMessages: vi.fn(),
    sendMessage: vi.fn(),
    streamUrl: vi.fn(() => 'ws://127.0.0.1:8000/api/share/tok/stream/run-1'),
  },
}))

const mockedApi = vi.mocked(shareChatApi)

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onmessage?: (event: { data: string }) => void
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  close() {}
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/share/tok']}>
      <Routes>
        <Route path="/share/:token" element={<ShareChatPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ShareChatPage', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
    mockedApi.getMessages.mockResolvedValue({ messages: [] })
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('loads existing history on mount', async () => {
    mockedApi.getMessages.mockResolvedValue({
      messages: [{ role: 'user', content: 'hi', turn_number: 1 }, { role: 'assistant', content: 'hello!', turn_number: 2 }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('hello!')).toBeInTheDocument())
  })

  it('sends a message, shows a friendly status, then the reply', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalledWith('tok', 'hi there'))
    expect(screen.getByText('hi there')).toBeInTheDocument()
    expect(await screen.findByText(/sending your message|working on/i)).toBeInTheDocument()

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'run_completed', agent: null, data: 'General Kenobi!', usage: [] })
    })
    expect(await screen.findByText('General Kenobi!')).toBeInTheDocument()
  })

  it('shows a friendly message when the link is unavailable', async () => {
    mockedApi.getMessages.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    renderPage()
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument()
  })
})
