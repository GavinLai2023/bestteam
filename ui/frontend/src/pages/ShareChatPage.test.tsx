import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ShareChatPage from './ShareChatPage'
import { shareChatApi } from '../lib/shareChatApi'
import { setLanguage } from '../lib/i18n'

vi.mock('../lib/shareChatApi', () => ({
  shareChatApi: {
    getMessages: vi.fn(),
    getTeam: vi.fn(),
    sendMessage: vi.fn(),
    sendFeedback: vi.fn(),
    cancelRun: vi.fn(),
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
    // Default: a team whose shape gives no honest step denominator, so the
    // existing tests see the pulse rather than dots they never asserted on.
    mockedApi.getTeam.mockResolvedValue({ name: 'Team', steps: null })
    mockedApi.cancelRun.mockResolvedValue({ cancelled: true })
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
    setLanguage('en')
  })

  it('switches the page to Chinese from its own language control', async () => {
    renderPage()
    await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    await waitFor(() => expect(screen.getByPlaceholderText('输入消息…')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument()
  })

  it("renders the backend-persisted fallback reply in the visitor's language", async () => {
    // The backend stores its fallback reply in English; a Chinese visitor
    // must not see an English sentence in the middle of their conversation.
    mockedApi.getMessages.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', turn_number: 1 },
        { role: 'assistant', content: 'Sorry, something went wrong producing a reply.', turn_number: 2 },
      ],
    })
    setLanguage('zh-CN')
    renderPage()
    expect(await screen.findByText('抱歉，生成回复时出了点问题。')).toBeInTheDocument()
  })

  it('shows the live status line in the visitor language', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    setLanguage('zh-CN')
    renderPage()
    const input = await screen.findByPlaceholderText('输入消息…')
    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText('正在发送…')).toBeInTheDocument()
  })

  it('re-renders a visible notice when the language changes', async () => {
    // Notices are stored as keys, not translated text, so a switch mid-notice
    // doesn't leave one sentence in the old language (Codex review).
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('[]'), { status: 422 }))
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'way too long' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByText(/^that message is too long/i)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    await waitFor(() => expect(screen.getByText(/消息太长了/)).toBeInTheDocument())
  })

  it('does not echo the backend detail of a 409 on this public page', async () => {
    mockedApi.sendMessage.mockRejectedValue(
      Object.assign(new Error('internal: lock held by worker bestteam-run_7'), { status: 409 }),
    )
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByText(/please wait for the previous reply to finish/i)).toBeInTheDocument()
    expect(screen.queryByText(/lock held/)).not.toBeInTheDocument()
  })

  it('keeps the language control on the unavailable page', async () => {
    mockedApi.getMessages.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    renderPage()
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })
    await waitFor(() => expect(screen.getByText('这个分享链接已失效。')).toBeInTheDocument())
  })

  it('sends on Enter and keeps Shift+Enter for a new line', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'line one' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(mockedApi.sendMessage).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalledWith('tok', 'line one'))
  })

  it('does not send on the Enter that confirms an IME candidate', async () => {
    // A Chinese/Japanese IME uses Enter to commit the composed text; that
    // keydown must not fire a send, or the visitor sends half a sentence.
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()
    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: '你好' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    expect(mockedApi.sendMessage).not.toHaveBeenCalled()
  })

  it('offers to copy an assistant reply, but not a user message', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    mockedApi.getMessages.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', turn_number: 1 },
        { role: 'assistant', content: 'hello!', turn_number: 2 },
      ],
    })
    renderPage()
    await screen.findByText('hello!')
    const copyButtons = screen.getAllByRole('button', { name: /^copy$/i })
    expect(copyButtons).toHaveLength(1)
    fireEvent.click(copyButtons[0])
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello!'))
    expect(await screen.findByRole('button', { name: /copied/i })).toBeInTheDocument()
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

  it('says a stopped turn was stopped, not that it failed', async () => {
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

    // The backend persists the "stopped" line for a cancellation, not the
    // generic failure one -- a reload must not disagree with the live view.
    expect(await screen.findByText(/stopped before a reply was ready/i)).toBeInTheDocument()
  })

  it('rolls the optimistic user bubble back when the send fails', async () => {
    // 503 (not 500): 500 specifically means the backend's dispatch-failure
    // path, which persists the turn server-side and gets its own refetch
    // behavior (see the dedicated 500 test below) -- this test covers every
    // other failure, where nothing was persisted and the rollback applies.
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('boom'), { status: 503 }))
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
    // Anchored: the composer is a <textarea>, whose restored draft ("way too
    // long") is itself text content a looser /too long/ would also match.
    expect(await screen.findByText(/^that message is too long/i)).toBeInTheDocument()
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
    // The run and this turn still exist server-side even though the socket
    // closed with no terminal event (registry eviction, a transient
    // connection drop) -- a reply may already have landed there, so onclose
    // must refetch rather than just re-enabling the input (Codex review
    // finding).
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    mockedApi.getMessages
      .mockResolvedValueOnce({ messages: [] }) // initial mount fetch
      .mockResolvedValueOnce({
        messages: [
          { role: 'user', content: 'hi there', turn_number: 1 },
          { role: 'assistant', content: 'answer that arrived after all', turn_number: 2 },
        ],
      })
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
    // The refetch surfaces the reply that actually landed while the socket
    // was down, instead of leaving it invisible until a page reload.
    await waitFor(() => expect(mockedApi.getMessages).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('answer that arrived after all')).toBeInTheDocument()
  })

  it('shows a friendly error and re-enables the form when sendMessage fails for a reason other than 429/404', async () => {
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('boom'), { status: 503 }))
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(await screen.findByText(/something went wrong sending your message/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/type a message/i)).not.toBeDisabled()
  })

  it('refetches instead of rolling back when a 500 means the turn was already recorded', async () => {
    // Unlike every other failure, the backend persists BOTH the user's
    // message and a fallback assistant reply when dispatch itself fails
    // (share_chat.py's executor.submit failure path) -- rolling the
    // optimistic bubble back here would let a retry duplicate an
    // already-recorded turn (Codex review finding).
    mockedApi.sendMessage.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    mockedApi.getMessages
      .mockResolvedValueOnce({ messages: [] }) // initial mount fetch
      .mockResolvedValueOnce({
        messages: [
          { role: 'user', content: 'hi there', turn_number: 1 },
          {
            role: 'assistant',
            content: "Couldn't start a reply just now. Please try sending your message again.",
            turn_number: 2,
          },
        ],
      })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.getMessages).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/couldn't start a reply/i)).toBeInTheDocument()
    // The recorded turn stays -- not rolled back, not duplicated.
    expect(screen.getAllByText('hi there')).toHaveLength(1)
  })

  it('does not let a slow initial history fetch overwrite a message sent before it resolves', async () => {
    // The mount-time getMessages() is a snapshot taken at mount -- if it's
    // still in flight when the visitor sends a message, its (now stale)
    // resolution must not wipe out what handleSend already put in state
    // (Codex review finding).
    let resolveInitial: (value: unknown) => void = () => {}
    const initialRequest = new Promise((resolve) => {
      resolveInitial = resolve
    })
    mockedApi.getMessages.mockReturnValueOnce(initialRequest as ReturnType<typeof mockedApi.getMessages>)
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())
    expect(screen.getByText('hi there')).toBeInTheDocument()

    await act(async () => {
      resolveInitial({ messages: [] })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText('hi there')).toBeInTheDocument()
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

  it('renders a reply as its deltas arrive, then replaces it with the final text', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'reply_delta', agent: null, data: 'Hel', usage: [] })
      ws.emit({ type: 'reply_delta', agent: null, data: 'lo', usage: [] })
    })
    expect(await screen.findByText('Hello')).toBeInTheDocument()

    // The streamed text is only ever a preview -- run_completed carries the
    // authoritative reply and replaces it outright.
    await act(async () => {
      ws.emit({ type: 'run_completed', agent: null, data: 'Hello, colleague.', usage: [] })
    })
    expect(await screen.findByText('Hello, colleague.')).toBeInTheDocument()
    expect(screen.queryByText('Hello')).not.toBeInTheDocument()
  })

  it('clears a partial reply when the backend takes it back', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(mockedApi.sendMessage).toHaveBeenCalled())

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'reply_delta', agent: null, data: 'Looking', usage: [] })
    })
    expect(await screen.findByText('Looking')).toBeInTheDocument()

    await act(async () => {
      ws.emit({ type: 'reply_reset', agent: null, data: null, usage: [] })
    })
    await waitFor(() => expect(screen.queryByText('Looking')).not.toBeInTheDocument())
  })

  it('shows step progress while a turn is in flight', async () => {
    mockedApi.getTeam.mockResolvedValue({ name: 'Support Team', steps: 2 })
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(await screen.findByText('Step 1 of 2')).toBeInTheDocument()

    const ws = FakeWebSocket.instances.at(-1)!
    await act(async () => {
      ws.emit({ type: 'agent_completed', agent: null, data: null, usage: [] })
    })
    expect(await screen.findByText('Step 2 of 2')).toBeInTheDocument()
  })

  it('lets the visitor stop a turn in flight', async () => {
    mockedApi.sendMessage.mockResolvedValue({ run_id: 'run-1', turn_number: 1 })
    renderPage()

    const input = await screen.findByPlaceholderText(/type a message/i)
    fireEvent.change(input, { target: { value: 'hi there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    const stop = await screen.findByRole('button', { name: /^stop$/i })
    fireEvent.click(stop)

    await waitFor(() => expect(mockedApi.cancelRun).toHaveBeenCalledWith('tok', 'run-1'))
  })

  it('offers Stop only while a turn is actually in flight', async () => {
    renderPage()
    await screen.findByPlaceholderText(/type a message/i)
    expect(screen.queryByRole('button', { name: /^stop$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send/i })).toBeInTheDocument()
  })

  it('names the team in the header', async () => {
    mockedApi.getTeam.mockResolvedValue({ name: 'Support Team', steps: 2 })
    renderPage()

    expect(await screen.findByText('Support Team')).toBeInTheDocument()
  })

  it('falls back to the product name when the team lookup fails', async () => {
    mockedApi.getTeam.mockRejectedValue(new Error('nope'))
    renderPage()

    await screen.findByPlaceholderText(/type a message/i)
    expect(screen.getByText('bestteam')).toBeInTheDocument()
  })

  it('lets a visitor send feedback from the header', async () => {
    mockedApi.sendFeedback.mockResolvedValue({ id: 1 })
    renderPage()
    await screen.findByPlaceholderText(/type a message/i)

    fireEvent.click(screen.getByRole('button', { name: /feedback/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByLabelText(/make a suggestion/i))
    fireEvent.change(within(dialog).getByLabelText(/your feedback/i), {
      target: { value: 'more emoji please' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /^send$/i }))

    await waitFor(() => expect(within(dialog).getByText(/thank you/i)).toBeInTheDocument())
    expect(mockedApi.sendFeedback).toHaveBeenCalledWith('tok', {
      kind: 'suggestion',
      body: 'more emoji please',
      context: expect.objectContaining({ page: '/share', locale: 'en' }),
    })
  })
})
