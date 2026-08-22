import { KeyboardEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import LanguageSelect from '../components/LanguageSelect'
import { shareChatApi } from '../lib/shareChatApi'
import { FALLBACK_REPLY, fallbackReplyKey, friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, TraceEvent } from '../lib/types'
import './ShareChatPage.css'

const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

const MAX_MESSAGE_LENGTH = 4000 // matches share_chat.py's own cap

// A transient notice under the conversation. Stored as a key (plus
// interpolation values) and translated at render, so a language switch while
// it is showing re-renders it like everything else (Codex review). A 409's
// backend detail is deliberately NOT shown: this is a public surface and the
// key says the same thing in the visitor's language.
interface Notice {
  key: 'share.recovered' | 'share.pendingTurn' | 'share.tooLong' | 'share.sendFailed' | 'common.copyFailed'
  values?: { max: number }
}

// The public, anonymous, multi-turn counterpart to MonitorPage's one-shot
// "Run a team" -- a colleague reaches this page via a link an org member
// generated (ShareLinksPanel), never logs in, and gets a real back-and-forth
// conversation. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md and, for this page's
// bilingual/mobile pass, 2026-08-22-share-chat-beta-patch-design.md.
export default function ShareChatPage() {
  const { t } = useTranslation()
  const { token = '' } = useParams<{ token: string }>()
  const [messages, setMessages] = useState<ShareMessage[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [liveEvents, setLiveEvents] = useState<TraceEvent[]>([])
  // An i18n key, translated at render so a language switch re-renders it.
  const [unavailableKey, setUnavailableKey] = useState<'share.unavailable' | 'share.loadFailed' | null>(null)
  const [rateLimited, setRateLimited] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  // Set inside onmessage's own terminal-event branches; onclose checks it to
  // tell "the stream ended normally, after a terminal event" apart from the
  // backend's real close-without-terminal-event paths (share_chat.py: an
  // evicted subscriber queue, or the link/org going inactive mid-stream) --
  // without this a visitor is left staring at a "Working on it..." line
  // forever with no way to recover short of reloading (review finding).
  const terminalSeenRef = useRef(false)
  // Set at the start of handleSend, before this effect's fetch can possibly
  // resolve. The initial history fetch is a snapshot taken at mount time --
  // if the visitor sends a message while it's still in flight, its (now
  // stale) resolution must not overwrite the optimistic message/live reply
  // handleSend has already put in state (Codex review finding).
  const hasSentRef = useRef(false)

  useEffect(() => {
    let ignore = false
    shareChatApi
      .getMessages(token)
      .then((data) => {
        if (ignore || hasSentRef.current) return
        setMessages(data.messages)
      })
      .catch((e: Error & { status?: number }) => {
        if (ignore || hasSentRef.current) return
        setUnavailableKey(e.status === 404 ? 'share.unavailable' : 'share.loadFailed')
      })
    return () => {
      ignore = true
    }
  }, [token])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, liveEvents])

  useEffect(() => {
    return () => {
      wsRef.current?.close()
    }
  }, [])

  // Shared by the form's submit and the composer's Enter key, hence the
  // structural parameter type rather than a FormEvent.
  const handleSend = async (event: { preventDefault(): void }) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || sending) return

    hasSentRef.current = true
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
          // until they happen to reload the page. Stored as the backend's
          // English literal (what a reload would return) and translated at
          // render by fallbackReplyKey, the same way a reloaded one is.
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
            setNotice({ key: 'share.recovered' })
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
        setUnavailableKey('share.unavailable')
      } else if (status === 409) {
        // The message was never persisted/no run was created for it (the
        // previous reply is still in flight) -- say so in the visitor's
        // language rather than echoing the backend's English detail.
        setNotice({ key: 'share.pendingTurn' })
      } else if (status === 422) {
        // The backend's length cap (Pydantic validation) -- its own detail is
        // a validation-error structure, not a sentence a visitor can read.
        setNotice({ key: 'share.tooLong', values: { max: MAX_MESSAGE_LENGTH } })
      } else {
        setNotice({ key: 'share.sendFailed' })
      }
      setDraft(content)
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey) return
    // An IME (Chinese, Japanese...) uses Enter to commit the candidate text;
    // that keydown arrives with isComposing set (keyCode 229 in older
    // browsers) and must not send a half-composed message.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return
    e.preventDefault()
    void handleSend(e)
  }

  const handleCopy = async (index: number, content: string) => {
    // `navigator.clipboard` rejects (or is undefined) in a non-secure
    // context -- any HTTP origin that isn't localhost -- so this can't be
    // left unguarded (same as ShareLinksPanel).
    try {
      await navigator.clipboard.writeText(content)
      setCopiedIndex(index)
      setTimeout(() => setCopiedIndex(null), 2000)
    } catch {
      setNotice({ key: 'common.copyFailed' })
    }
  }

  // The header (brand + language control) renders on the unavailable page
  // too: a visitor who lands on an expired link in a language they can't
  // read must still be able to switch (Codex review).
  const header = (
    <header className="share-chat-header">
      <span className="share-chat-brand">{t('nav.brand')}</span>
      <LanguageSelect />
    </header>
  )

  if (unavailableKey) {
    return (
      <div className="share-chat">
        {header}
        <p className="share-chat-unavailable">{t(unavailableKey)}</p>
      </div>
    )
  }

  return (
    <div className="share-chat">
      {header}
      <div className="share-chat-messages">
        {messages.map((m, i) => {
          if (m.role === 'user') {
            return (
              <div key={i} className="share-chat-bubble user">
                {m.content}
              </div>
            )
          }
          // A wrapper so the copy control sits under the bubble, not inside
          // it -- the bubble keeps its text alone.
          const key = fallbackReplyKey(m.content)
          return (
            <div key={i} className="share-chat-assistant">
              <div className="share-chat-bubble assistant">{key ? t(key) : m.content}</div>
              {!key && (
                <button type="button" className="btn-link share-chat-copy" onClick={() => void handleCopy(i, m.content)}>
                  {copiedIndex === i ? t('common.copied') : t('common.copy')}
                </button>
              )}
            </div>
          )
        })}
        {/* A polite live region, so assistive technology hears the progress
            line and any notice change without the focus moving. */}
        <div role="status" aria-live="polite">
          {sending && <div className="share-chat-bubble status">{t(friendlyStatusFor(liveEvents))}</div>}
        </div>
        <div ref={messagesEndRef} />
      </div>
      <div role="status" aria-live="polite">
        {rateLimited && <p className="share-chat-bubble status">{t('share.rateLimited')}</p>}
        {notice && <p className="share-chat-bubble status">{t(notice.key, notice.values)}</p>}
      </div>
      <form className="share-chat-form" onSubmit={handleSend}>
        <textarea
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={t('share.placeholder')}
          aria-label={t('share.composerLabel')}
          aria-describedby="share-chat-hint"
          disabled={sending || rateLimited}
        />
        <button type="submit" disabled={sending || rateLimited || !draft.trim()}>
          {t('share.send')}
        </button>
      </form>
      <p id="share-chat-hint" className="hint share-chat-hint">
        {t('share.sendHint')}
      </p>
    </div>
  )
}
