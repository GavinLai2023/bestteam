import { useTranslation } from 'react-i18next'
import type { TraceEvent } from './types'

// Shared trace-event rendering for MonitorPage's live view and the Activity
// page's run-detail view (live and historical), so both render the same
// event stream identically instead of duplicating the mapping.
//
// Two registers live here on purpose. `EVENT_LABELS` below is the technical
// one (`✓ agent done`), for the collapsed "technical trace". Above it,
// `useFriendlyEventTitle` is the one a customer reads by default -- it was
// previously inlined in the wizard's PreviewPage, which meant the same event
// stream was narrated politely during the wizard and in jargon everywhere
// afterwards (audit finding F8).

// Narrates an event in the customer's own terms. `displayNameFor` resolves an
// agent's technical name to its friendly `display_name`; pass through the
// agent name itself when there is no specification to resolve against.
export function useFriendlyEventTitle(displayNameFor: (agentName: string) => string) {
  const { t } = useTranslation()
  return (event: TraceEvent): string => {
    switch (event.type) {
      case 'run_queued':
        return t('traceEvents.queued')
      case 'run_started':
        return t('traceEvents.started')
      case 'agent_completed':
        return t('traceEvents.agentDone', { agent: displayNameFor(event.agent ?? '') })
      case 'run_completed':
        return t('traceEvents.completed')
      case 'run_failed':
        return t('traceEvents.failed')
      case 'run_cancelled':
        return t('traceEvents.cancelled')
      default:
        // Intermediate types (tool_*, delegation_*, memory_*) have no friendly
        // phrasing; the caller filters them out of the friendly view rather
        // than this returning an invented sentence for them.
        return event.type
    }
  }
}

// The event types the friendly view knows how to narrate. Anything else is
// detail that belongs in the technical trace, not on a customer's screen.
export const FRIENDLY_EVENT_TYPES = [
  'run_queued',
  'run_started',
  'agent_completed',
  'run_completed',
  'run_failed',
  'run_cancelled',
]

export const EVENT_LABELS: Record<string, string> = {
  run_queued: '⏳ queued',
  run_started: '▶ started',
  agent_started: '● agent started',
  agent_progress: '… agent progress',
  tool_started: '🔧 tool started',
  tool_completed: '🔧 tool completed',
  delegation_started: '↳ delegating',
  subagent_started: '● sub-agent started',
  subagent_completed: '✓ sub-agent done',
  delegation_completed: '↳ delegation done',
  agent_completed: '✓ agent done',
  run_completed: '● completed',
  run_failed: '✕ failed',
  run_cancelled: '■ cancelled',
  memory_recalled: '🧠 memory recalled',
  memory_recorded: '🧠 memory recorded',
  memory_failed: '🧠 memory failed',
}

export const TERMINAL_TYPES = ['run_completed', 'run_failed', 'run_cancelled']

export const RESULT_LABELS: Record<string, string> = {
  run_completed: 'Final output',
  run_failed: 'Run failed',
  run_cancelled: 'Run cancelled',
}

// Several event types carry an object `data` (agent_started, tool_started/
// completed, agent_progress, delegation_*, subagent_*) instead of a plain
// string -- render each shape sensibly rather than dumping raw JSON.
export function renderEventData(event: TraceEvent): string | null {
  const { type, data } = event
  if (data === null || data === undefined) return null
  if (typeof data !== 'object') return data
  switch (type) {
    case 'agent_started':
      return [data.role as string | undefined, data.goal as string | undefined].filter(Boolean).join(' — ')
    case 'agent_progress':
      return (data.note as string | undefined) ?? null
    case 'tool_started':
      return (data.tool as string | undefined) ?? null
    case 'tool_completed': {
      const parts = [data.tool, data.success ? 'success' : 'failed']
      if (typeof data.duration_ms === 'number') parts.push(`${data.duration_ms}ms`)
      return [parts.join(' · '), data.summary as string | undefined].filter(Boolean).join(' — ')
    }
    case 'delegation_started':
    case 'delegation_completed':
      return [data.to as string | undefined, (data.task_summary ?? data.summary) as string | undefined]
        .filter(Boolean)
        .join(' — ')
    case 'subagent_started':
    case 'subagent_completed':
      return ((data.task_summary ?? data.summary) as string | undefined) ?? null
    default:
      return JSON.stringify(data)
  }
}
