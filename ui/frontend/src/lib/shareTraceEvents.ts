import type { TraceEvent } from './types'

// A visitor chat page shows a short, non-technical progress line instead of
// the raw trace `lib/traceEvents.ts` renders for the logged-in Activity
// page -- deliberately generic (never a raw tool/agent name), since a
// colleague using a shared link shouldn't see the team's internal wiring.
// Values are i18n keys under `share.status` (locales/en.ts); the page
// translates them, so this module stays free of react-i18next. Typed as a
// literal union because `t()` is typed against the locale keys.
export type ShareStatusKey =
  | 'share.status.sending'
  | 'share.status.starting'
  | 'share.status.working'
  | 'share.status.checking'
  | 'share.status.composing'
  | 'share.status.default'

const FRIENDLY_STATUS: Record<string, ShareStatusKey> = {
  run_queued: 'share.status.sending',
  run_started: 'share.status.starting',
  agent_started: 'share.status.working',
  agent_progress: 'share.status.working',
  tool_started: 'share.status.working',
  tool_completed: 'share.status.working',
  delegation_started: 'share.status.checking',
  subagent_started: 'share.status.checking',
  subagent_completed: 'share.status.checking',
  delegation_completed: 'share.status.composing',
  agent_completed: 'share.status.composing',
}

const DEFAULT_STATUS: ShareStatusKey = 'share.status.default'
const INITIAL_STATUS: ShareStatusKey = 'share.status.sending'

export function friendlyStatusFor(events: TraceEvent[]): ShareStatusKey {
  if (events.length === 0) return INITIAL_STATUS
  const last = events[events.length - 1]
  return FRIENDLY_STATUS[last.type] ?? DEFAULT_STATUS
}

// The backend persists these two assistant replies in English:
// share_transcript.py `_FALLBACK_REPLY` (a failed/cancelled/crashed run) and
// share_chat.py `_DISPATCH_FAILED_MESSAGE` (the executor refused the run).
// They come back verbatim in GET .../messages, so the page recognises them
// by value and renders the visitor's language instead. A deliberate,
// brittle string-equality coupling -- change either literal in lockstep
// with the backend (docs/STATUS.md, Known issues).
export const FALLBACK_REPLY = 'Sorry, something went wrong producing a reply.'
export const DISPATCH_FAILED_REPLY = "Couldn't start a reply just now. Please try sending your message again."
// runtime.py's `_mark_cancelled` persists this one when a visitor stops a turn
// (or an operator cancels the run).
export const STOPPED_REPLY = 'This conversation was stopped before a reply was ready.'

export type FallbackReplyKey = 'share.fallbackReply' | 'share.dispatchFailedReply' | 'share.stoppedReply'

const FALLBACK_REPLY_KEYS: Record<string, FallbackReplyKey> = {
  [FALLBACK_REPLY]: 'share.fallbackReply',
  [DISPATCH_FAILED_REPLY]: 'share.dispatchFailedReply',
  [STOPPED_REPLY]: 'share.stoppedReply',
}

export function fallbackReplyKey(content: string): FallbackReplyKey | null {
  return FALLBACK_REPLY_KEYS[content] ?? null
}
