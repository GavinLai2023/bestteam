import { FormEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { shareChatApi } from '../lib/shareChatApi'
import { friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, TraceEvent } from '../lib/types'
import './ShareChatPage.css'

const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

// The public, anonymous, multi-turn counterpart to MonitorPage's one-shot
// "Run a team" -- a colleague reaches this page via a link an org member
// generated (ShareLinksPanel), never logs in, and gets a real back-and-forth
// conversation. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md.
export default function ShareChatPage() {
  const { token = '' } = useParams<{ token: string }>()
  const [messages, setMessages] = useState<ShareMessage[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [liveEvents, setLiveEvents] = useState<TraceEvent[]>([])
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const [rateLimited, setRateLimited] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  // Set inside onmessage's own terminal-event branches; onclose checks it to
  // tell "the stream ended normally, after a terminal event" apart from the
  // backend's real close-without-terminal-event paths (share_chat.py: an
  // evicted subscriber queue, or the link/org going inactive mid-stream) --
  // without this a visitor is left staring at a "Working on it..." line
  // forever with no way to recover short of reloading (review finding).
  const terminalSeenRef = useRef(false)

  useEffect(() => {
    shareChatApi
      .getMessages(token)
      .then((data) => setMessages(data.messages))
      .catch((e: Error & { status?: number }) => {
        setUnavailable(
          e.status === 404
            ? 'This share link is no longer available.'
            : "Couldn't load this conversation.",
        )
      })
  }, [token])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, liveEvents])

  useEffect(() => {
    return () => {
      wsRef.current?.close()
    }
  }, [])

  const handleSend = async (event: FormEvent) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || sending) return

    setSending(true)
    setRateLimited(false)
    setNotice(null)
    setDraft('')
    setMessages((prev) => [...prev, { role: 'user', content, turn_number: prev.length + 1 }])
    setLiveEvents([])
    terminalSeenRef.current = false

    try {
      const { run_id: runId } = await shareChatApi.sendMessage(token, content)
      const ws = new WebSocket(shareChatApi.streamUrl(token, runId))
      wsRef.current = ws
      ws.onmessage = (msg: MessageEvent<string>) => {
        const traceEvent = JSON.parse(msg.data) as TraceEvent
        setLiveEvents((prev) => [...prev, traceEvent])
        if (traceEvent.type === 'run_completed') {
          terminalSeenRef.current = true
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: String(traceEvent.data ?? ''), turn_number: prev.length + 1 },
          ])
          setSending(false)
        } else if (TERMINAL_TYPES.includes(traceEvent.type)) {
          terminalSeenRef.current = true
          setSending(false)
        }
      }
      ws.onerror = () => setSending(false)
      // onclose always fires, including right after a clean terminal event
      // onmessage already handled -- only show a recovery notice when the
      // socket closed WITHOUT one (evicted queue, or the link/org going
      // inactive mid-stream both close with no terminal event at all).
      ws.onclose = () => {
        if (!terminalSeenRef.current) {
          setSending(false)
          setNotice('Something went wrong. Please try sending your message again.')
        }
      }
    } catch (e) {
      const status = (e as Error & { status?: number }).status
      setSending(false)
      if (status === 429) {
        setRateLimited(true)
      } else if (status === 404) {
        setUnavailable('This share link is no longer available.')
      } else if (status === 409) {
        // The message was never persisted/no run was created for it -- the
        // backend's own detail text already says what to do.
        setNotice((e as Error).message || 'Please wait for the previous reply to finish.')
      } else {
        setNotice('Something went wrong sending your message. Please try again.')
      }
    }
  }

  if (unavailable) {
    return (
      <div className="share-chat">
        <p className="share-chat-unavailable">{unavailable}</p>
      </div>
    )
  }

  return (
    <div className="share-chat">
      <div className="share-chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`share-chat-bubble ${m.role}`}>
            {m.content}
          </div>
        ))}
        {sending && <div className="share-chat-bubble status">{friendlyStatusFor(liveEvents)}</div>}
        <div ref={messagesEndRef} />
      </div>
      {rateLimited && (
        <p className="share-chat-bubble status">Today's message limit has been reached — try again tomorrow.</p>
      )}
      {notice && <p className="share-chat-bubble status">{notice}</p>}
      <form className="share-chat-form" onSubmit={handleSend}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type a message…"
          disabled={sending || rateLimited}
        />
        <button type="submit" disabled={sending || rateLimited || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
