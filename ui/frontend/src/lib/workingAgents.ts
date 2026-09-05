import { useMemo } from 'react'
import { TERMINAL_TYPES } from './traceEvents'
import type { TraceEvent } from './types'

export interface WorkingAgent {
  agent: string
  kind: 'agent' | 'subagent'
}

export interface WorkingAgents {
  // In start order. More than one at once means a parallel team.
  working: WorkingAgent[]
  // Persisted top-level completions so far -- the "k" in "agent k of N".
  completedAgents: number
}

function remove(working: WorkingAgent[], agent: string) {
  const index = working.findIndex((w) => w.agent === agent)
  if (index >= 0) working.splice(index, 1)
}

// Who is working right now, from the live `agent_working` milestone and the
// persisted completions that end it (spec 2026-09-05). Derived from the whole
// event list rather than kept as state, so a replay on reconnect and a live
// stream produce the same answer.
export function deriveWorkingAgents(events: TraceEvent[]): WorkingAgents {
  const working: WorkingAgent[] = []
  let completedAgents = 0
  for (const event of events) {
    if (TERMINAL_TYPES.includes(event.type)) {
      working.length = 0
      continue
    }
    const agent = event.agent
    if (!agent) continue
    if (event.type === 'agent_working') {
      const data = (event.data ?? {}) as { kind?: string; state?: string }
      if (data.state === 'completed') {
        remove(working, agent)
      } else if (!working.some((w) => w.agent === agent)) {
        working.push({ agent, kind: data.kind === 'subagent' ? 'subagent' : 'agent' })
      }
    } else if (event.type === 'agent_completed') {
      remove(working, agent)
      completedAgents += 1
    } else if (event.type === 'subagent_completed') {
      remove(working, agent)
    }
  }
  return { working, completedAgents }
}

export function useWorkingAgents(events: TraceEvent[]): WorkingAgents {
  return useMemo(() => deriveWorkingAgents(events), [events])
}
