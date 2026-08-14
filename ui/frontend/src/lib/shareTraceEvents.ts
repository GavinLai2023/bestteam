import type { TraceEvent } from './types'

// A visitor chat page shows a short, non-technical progress line instead of
// the raw trace `lib/traceEvents.ts` renders for the logged-in Activity
// page -- deliberately generic (never a raw tool/agent name), since a
// colleague using a shared link shouldn't see the team's internal wiring.
const FRIENDLY_STATUS: Record<string, string> = {
  run_queued: 'Sending your message…',
  run_started: 'Getting started…',
  agent_started: 'Working on your question…',
  agent_progress: 'Working on your question…',
  tool_started: 'Working on your question…',
  tool_completed: 'Working on your question…',
  delegation_started: 'Checking with the team…',
  subagent_started: 'Checking with the team…',
  subagent_completed: 'Checking with the team…',
  delegation_completed: 'Putting together a reply…',
  agent_completed: 'Putting together a reply…',
}

const DEFAULT_STATUS = 'Working on it…'
const INITIAL_STATUS = 'Sending your message…'

export function friendlyStatusFor(events: TraceEvent[]): string {
  if (events.length === 0) return INITIAL_STATUS
  const last = events[events.length - 1]
  return FRIENDLY_STATUS[last.type] ?? DEFAULT_STATUS
}
