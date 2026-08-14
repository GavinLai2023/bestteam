import { describe, it, expect } from 'vitest'
import { friendlyStatusFor } from './shareTraceEvents'
import type { TraceEvent } from './types'

describe('friendlyStatusFor', () => {
  it('returns a generic starting phrase with no events yet', () => {
    expect(friendlyStatusFor([])).toBe('Sending your message…')
  })

  it('maps the most recent known event type to a friendly phrase', () => {
    const events: TraceEvent[] = [
      { type: 'run_started', agent: undefined, data: null },
      { type: 'tool_started', agent: 'a', data: { tool: 'web_search' } },
    ]
    expect(friendlyStatusFor(events)).toBe('Working on your question…')
  })

  it('never leaks a raw tool or agent name into the phrase', () => {
    const events: TraceEvent[] = [
      { type: 'tool_started', agent: 'a', data: { tool: 'email_find' } },
    ]
    expect(friendlyStatusFor(events)).not.toMatch(/email_find/)
  })

  it('falls back to a generic phrase for an unmapped event type', () => {
    const events: TraceEvent[] = [{ type: 'some_future_event', agent: undefined, data: null }]
    expect(friendlyStatusFor(events)).toBe('Working on it…')
  })
})
