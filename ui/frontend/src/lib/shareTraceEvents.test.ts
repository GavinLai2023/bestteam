import { describe, it, expect } from 'vitest'
import {
  DISPATCH_FAILED_REPLY,
  FALLBACK_REPLY,
  STOPPED_REPLY,
  fallbackReplyKey,
  friendlyStatusFor,
} from './shareTraceEvents'
import type { TraceEvent } from './types'

describe('friendlyStatusFor', () => {
  it('returns the sending key with no events yet', () => {
    expect(friendlyStatusFor([])).toBe('share.status.sending')
  })

  it('maps the most recent known event type to a status key', () => {
    const events: TraceEvent[] = [
      { type: 'run_started', agent: undefined, data: null },
      { type: 'tool_started', agent: 'a', data: { tool: 'web_search' } },
    ]
    expect(friendlyStatusFor(events)).toBe('share.status.working')
  })

  it('never leaks a raw tool or agent name', () => {
    const events: TraceEvent[] = [{ type: 'tool_started', agent: 'a', data: { tool: 'email_find' } }]
    expect(friendlyStatusFor(events)).not.toMatch(/email_find/)
  })

  it('maps the live agent_working milestone to the same "working" wording as agent_started', () => {
    // visitor_safe_event nulls agent/data for this type, so it carries no
    // more than agent_started already does here (spec 2026-09-05).
    const events: TraceEvent[] = [{ type: 'agent_working', agent: undefined, data: null }]
    expect(friendlyStatusFor(events)).toBe('share.status.working')
  })

  it('falls back to the default key for an unmapped event type', () => {
    const events: TraceEvent[] = [{ type: 'some_future_event', agent: undefined, data: null }]
    expect(friendlyStatusFor(events)).toBe('share.status.default')
  })
})

describe('fallbackReplyKey', () => {
  it('recognises the two replies the backend persists in English', () => {
    expect(fallbackReplyKey(FALLBACK_REPLY)).toBe('share.fallbackReply')
    expect(fallbackReplyKey(DISPATCH_FAILED_REPLY)).toBe('share.dispatchFailedReply')
    expect(fallbackReplyKey(STOPPED_REPLY)).toBe('share.stoppedReply')
  })

  it('leaves a real reply alone', () => {
    expect(fallbackReplyKey('Here is your answer.')).toBeNull()
  })
})
