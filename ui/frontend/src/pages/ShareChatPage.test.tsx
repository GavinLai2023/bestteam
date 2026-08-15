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
  onclose?: () => void
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  close() {}
  emit(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
  triggerClose() {
    this.onclose?.()
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

  it('shows a friendly reply bubble on run_failed instead of going silent', async () => {
    // The backend has already persisted its own fallback reply for a
    // failed/cancelled run, so a page reload would show one -- the live view
    // used to render nothing at all, leaving the message looking unanswered.
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'run_failed', agent: null, data: null, usage: [] })
    })

    expect(await screen.findByText(/something went wrong producing a reply/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/type a message/i)).not.toBeDisabled()
  })

  it('shows a friendly reply bubble on run_cancelled too', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'run_cancelled', agent: null, data: null, usage: [] })
    })

    expect(await screen.findByText(/something went wrong producing a reply/i)).toBeInTheDocument()
  })

  it('rolls the optimistic user bubble back when the send fails', async () => {
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'never persisted' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    // The server stored nothing, so nothing about this message may stay on
    // screen as though it had been sent (it used to linger until a reload).
    await waitFor(() =>
      expect(screen.queryByText('never persisted', { selector: '.share-chat-bubble' })).not.toBeInTheDocument(),
    )
    // The visitor's text is handed back to them rather than lost.
    expect(screen.getByPlaceholderText(/type a message/i)).toHaveValue('never persisted')
  })

  it('shows a specific message when the backend rejects an over-length message', async () => {
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('[]'), { status: 422 }))
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'way too long' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())
    expect(await screen.findByText(/too long/i)).toBeInTheDocument()
  })

  it('caps the input length in the browser itself', async () => {
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    expect(input).toHaveAttribute('maxlength', '4000')
  })

  it('shows a friendly message when the link is unavailable', async () => {
    mockedApi.getMessages.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    renderPage()
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument()
  })

  it('recovers when the websocket closes without ever sending a terminal event', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())
    expect(await screen.findByText(/sending your message|working on/i)).toBeInTheDocument()

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.triggerClose()
    })

    // The "working on it" status must clear, a recoverable message must
    // appear, and the visitor must be able to try again without reloading.
    // (Anchored so it doesn't also match the recovery notice's own text,
    // which itself contains the phrase "sending your message".)
    expect(screen.queryByText(/^(sending your message|working on)/i)).not.toBeInTheDocument()
    expect(await screen.findByText(/something went wrong/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/type a message/i)).not.toBeDisabled()
  })

  it('shows a friendly error and re-enables the form when sendMessage fails for a reason other than 429/404', async () => {
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(await screen.findByText(/something went wrong sending your message/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/type a message/i)).not.toBeDisabled()
  })

  it('shows the backend message and re-enables the form on a 409 (already-pending turn)', async () => {
    mockedApi.sendMessage.mockRejectedValue(
      Object.assign(new Error('Please wait for the previous reply to finish.'), { status: 409 }),
    )
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(await screen.findByText(/please wait for the previous reply to finish/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/type a message/i)).not.toBeDisabled()
  })
})
