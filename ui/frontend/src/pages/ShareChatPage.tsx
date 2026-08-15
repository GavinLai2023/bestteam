import { FormEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { shareChatApi } from '../lib/shareChatApi'
import { friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, TraceEvent } from '../lib/types'
import './ShareChatPage.css'

const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

// Mirrors share_transcript.py's `_FALLBACK_REPLY`, which the backend has
// already persisted for a failed/cancelled run by the time that terminal
// event arrives. Showing the same string keeps the screen consistent with
// what a page reload would render, without an extra round-trip.
const FALLBACK_REPLY = 'Sorry, something went wrong producing a reply.'

const MAX_MESSAGE_LENGTH = 4000 // matches share_chat.py's own cap

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
    // Held by reference so any send failure below can take it back off screen
    // -- the server never persisted it, so leaving it there showed the visitor
    // a message that silently vanishes on the next reload.
    const optimisticUserMessage: ShareMessage = {
      role: 'user',
      content,
      turn_number: messages.length + 1,
    }
    setMessages((prev) => [...prev, optimisticUserMessage])
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
          // run_failed / run_cancelled: the backend has already persisted its
          // own friendly fallback reply for this turn, so show the same thing
          // now instead of leaving the visitor's message looking unanswered
          // until they happen to reload the page.
          terminalSeenRef.current = true
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: FALLBACK_REPLY, turn_number: prev.length + 1 },
          ])
          setSending(false)
        }
      }
      ws.onerror = () => setSending(false)
      // onclose always fires, including right after a clean terminal event
      // onmessage already handled -- only act when the socket closed
      // WITHOUT one (evicted queue, or the link/org going inactive
      // mid-stream both close with no terminal event at all). The run and
      // this turn still exist server-side either way, and a reply may
      // already have landed there while this socket was down -- refetch
      // instead of just re-enabling the input, or the visitor's real answer
      // (or the friendly fallback the backend already recorded) stays
      // invisible until they happen to reload the page (Codex review
      // finding).
      ws.onclose = () => {
        if (terminalSeenRef.current) return
        shareChatApi
          .getMessages(token)
          .then((data) => setMessages(data.messages))
          .catch(() => {})
          .finally(() => {
            setSending(false)
            setNotice('Something went wrong. Please try sending your message again.')
          })
      }
    } catch (e) {
      const status = (e as Error & { status?: number }).status
      setSending(false)
      if (status === 500) {
        // Unlike every other failure here, the backend persists BOTH the
        // user's message and a fallback assistant reply even when dispatch
        // itself fails (share_chat.py's executor.submit failure path) --
        // rolling the optimistic bubble back and letting the visitor retype
        // would duplicate a turn that's already recorded server-side.
        // Refetch instead, so what's on screen matches the server exactly
        // (Codex review finding).
        shareChatApi
          .getMessages(token)
          .then((data) => setMessages(data.messages))
          .catch(() => {})
        return
      }
      // Every other failure means nothing was persisted for this send, so
      // the optimistic bubble must come back off screen -- otherwise what's
      // rendered disagrees with server state until a reload silently drops
      // it.
      setMessages((prev) => prev.filter((m) => m !== optimisticUserMessage))
      if (status === 429) {
        setRateLimited(true)
      } else if (status === 404) {
        setUnavailable('This share link is no longer available.')
      } else if (status === 409) {
        // The message was never persisted/no run was created for it -- the
        // backend's own detail text already says what to do.
        setNotice((e as Error).message || 'Please wait for the previous reply to finish.')
      } else if (status === 422) {
        // The backend's length cap (Pydantic validation) -- its own detail is
        // a validation-error structure, not a sentence a visitor can read.
        setNotice(`That message is too long. Please keep it under ${MAX_MESSAGE_LENGTH} characters.`)
      } else {
        setNotice('Something went wrong sending your message. Please try again.')
      }
      setDraft(content)
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
          maxLength={MAX_MESSAGE_LENGTH}
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
