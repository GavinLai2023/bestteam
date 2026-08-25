import { KeyboardEvent, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import FeedbackModal from '../components/FeedbackModal'
import LanguageSelect from '../components/LanguageSelect'
import MarkdownText from '../components/MarkdownText'
import ShareProgress from '../components/ShareProgress'
import { shareChatApi } from '../lib/shareChatApi'
import { FALLBACK_REPLY, STOPPED_REPLY, fallbackReplyKey, friendlyStatusFor } from '../lib/shareTraceEvents'
import type { ShareMessage, ShareTeamInfo, TraceEvent } from '../lib/types'
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
  const { t, i18n } = useTranslation()
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
  // The reply as it is being written. Only ever a preview: `run_completed`
  // discards it and appends the authoritative text instead, so nothing
  // partial is ever kept or persisted (see the step-2 streaming spec).
  const [streamedReply, setStreamedReply] = useState('')
  const [team, setTeam] = useState<ShareTeamInfo | null>(null)
  // The run this turn is streaming, so the visitor can stop it. Cleared on
  // every terminal path, so Stop can never target the previous turn.
  const [runId, setRunId] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [sendingFeedback, setSendingFeedback] = useState(false)
  // Unlike `runId` (cleared on every terminal path so Stop can't target the
  // previous turn), this survives turn completion: feedback usually arrives
  // AFTER the reply the visitor is reacting to.
  const lastRunIdRef = useRef<string | null>(null)
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
    let ignore = false
    shareChatApi
      .getTeam(token)
      .then((info) => {
        if (!ignore) setTeam(info)
      })
      // A failure here costs the header and the step count, not the chat --
      // the page falls back to the brand and an anonymous pulse.
      .catch(() => {})
    return () => {
      ignore = true
    }
  }, [token])

  useEffect(() => {
    // `streamedReply` belongs here as much as the other two: a long final
    // answer grows without any intervening agent event, so without it the
    // visitor is left behind the tokens until the turn completes (Codex
    // review finding).
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, liveEvents, streamedReply])

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
    setStreamedReply('')
    setRunId(null)
    setStopping(false)
    terminalSeenRef.current = false

    try {
      const { run_id: dispatchedRunId } = await shareChatApi.sendMessage(token, content)
      setRunId(dispatchedRunId)
      lastRunIdRef.current = dispatchedRunId
      const ws = new WebSocket(shareChatApi.streamUrl(token, dispatchedRunId))
      wsRef.current = ws
      ws.onmessage = (msg: MessageEvent<string>) => {
        const traceEvent = JSON.parse(msg.data) as TraceEvent
        if (traceEvent.type === 'reply_delta') {
          // Not a trace event as far as the page is concerned: it never joins
          // liveEvents, so the progress indicator keeps counting agents.
          setStreamedReply((prev) => prev + String(traceEvent.data ?? ''))
          return
        }
        if (traceEvent.type === 'reply_reset') {
          // The text so far belonged to a tool call, not the reply.
          setStreamedReply('')
          return
        }
        setLiveEvents((prev) => [...prev, traceEvent])
        if (traceEvent.type === 'run_completed') {
          terminalSeenRef.current = true
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: String(traceEvent.data ?? ''), turn_number: prev.length + 1 },
          ])
          setSending(false)
          setStreamedReply('')
          setRunId(null)
          setStopping(false)
        } else if (TERMINAL_TYPES.includes(traceEvent.type)) {
          // run_failed / run_cancelled: the backend has already persisted its
          // own friendly reply for this turn, so show the same thing now
          // instead of leaving the visitor's message looking unanswered until
          // they happen to reload the page. Stored as the backend's English
          // literal (what a reload would return) and translated at render by
          // fallbackReplyKey, the same way a reloaded one is -- and it is a
          // DIFFERENT literal for a stop than for a failure, or a reload
          // would contradict what the visitor just saw.
          terminalSeenRef.current = true
          const persisted = traceEvent.type === 'run_cancelled' ? STOPPED_REPLY : FALLBACK_REPLY
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: persisted, turn_number: prev.length + 1 },
          ])
          setSending(false)
          setStreamedReply('')
          setRunId(null)
          setStopping(false)
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
            setStreamedReply('')
            setRunId(null)
            setStopping(false)
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

  const handleStop = async () => {
    if (!runId || stopping) return
    setStopping(true)
    try {
      await shareChatApi.cancelRun(token, runId)
    } catch {
      // The run may have finished between the click and this call; the
      // terminal event that follows resolves the button either way.
      setStopping(false)
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
      <span className="share-chat-brand">{team?.name ?? t('nav.brand')}</span>
      <button type="button" className="btn-link" onClick={() => setSendingFeedback(true)}>
        {t('nav.feedback')}
      </button>
      <LanguageSelect />
    </header>
  )

  const feedbackModal = (
    <FeedbackModal
      open={sendingFeedback}
      onClose={() => setSendingFeedback(false)}
      onSubmit={async (kind, body) => {
        await shareChatApi.sendFeedback(token, {
          kind,
          body,
          context: {
            page: '/share',
            locale: i18n.language,
            ...(lastRunIdRef.current ? { run_id: lastRunIdRef.current } : {}),
          },
        })
      }}
    />
  )

  if (unavailableKey) {
    return (
      <div className="share-chat">
        {header}
        <p className="share-chat-unavailable">{t(unavailableKey)}</p>
        {feedbackModal}
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
              <div className="share-chat-bubble assistant">
                {key ? t(key) : <MarkdownText text={m.content} />}
              </div>
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
          {sending && streamedReply === '' && (
            <div className="share-chat-bubble status">{t(friendlyStatusFor(liveEvents))}</div>
          )}
        </div>
        {sending && streamedReply !== '' && (
          <div className="share-chat-assistant">
            <div className="share-chat-bubble assistant share-chat-streaming">
              {/* Half-written markdown renders as the text it currently is
                  and settles as it completes. */}
              <MarkdownText text={streamedReply} />
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      {sending && (
        <div className="share-chat-progress">
          <ShareProgress events={liveEvents} steps={team?.steps ?? null} />
        </div>
      )}
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
        {sending ? (
          <button type="button" onClick={() => void handleStop()} disabled={!runId || stopping}>
            {stopping ? t('share.stopping') : t('share.stop')}
          </button>
        ) : (
          <button type="submit" disabled={rateLimited || !draft.trim()}>
            {t('share.send')}
          </button>
        )}
      </form>
      <p id="share-chat-hint" className="hint share-chat-hint">
        {t('share.sendHint')}
      </p>
      {feedbackModal}
    </div>
  )
}
