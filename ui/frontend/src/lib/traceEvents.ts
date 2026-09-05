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

// One line of the customer's expanded run view. `detail` is the optional
// second line (an agent's role, a delegated task) -- absent, not empty, when
// there is nothing worth a second line.
export interface DetailedEventLine {
  title: string
  detail?: string
}

type Translate = ReturnType<typeof useTranslation>['t']

// The tool the adapter generates for each subordinate a manager can delegate
// to (`langgraph_adapter._delegation_tools` names it `delegate_to_<agent>`).
// Its tool_completed is a platform-internal identifier AND a duplicate of the
// delegation_started/completed pair, so the detailed view drops it.
const DELEGATION_TOOL_PREFIX = 'delegate_to_'

// A built-in tool's line, or null if this team declared the tool itself.
// A switch on literal keys rather than a computed `t()` argument: the key
// space is typed against locales/en.ts, so only literals type-check -- and a
// tool added to tools/__init__.py without a line here falls through to the
// custom-tool wording rather than leaking its identifier as a label.
function builtInToolTitle(t: Translate, tool: string, success: boolean): string | null {
  switch (tool) {
    case 'email_find':
      return success ? t('traceDetail.tools.emailFind') : t('traceDetail.tools.emailFindFailed')
    case 'email_read':
      return success ? t('traceDetail.tools.emailRead') : t('traceDetail.tools.emailReadFailed')
    case 'email_read_attachment':
      return success
        ? t('traceDetail.tools.emailReadAttachment')
        : t('traceDetail.tools.emailReadAttachmentFailed')
    case 'email_draft_reply':
      return success
        ? t('traceDetail.tools.emailDraftReply')
        : t('traceDetail.tools.emailDraftReplyFailed')
    case 'web_search':
      return success ? t('traceDetail.tools.webSearch') : t('traceDetail.tools.webSearchFailed')
    case 'parse_file':
      return success ? t('traceDetail.tools.parseFile') : t('traceDetail.tools.parseFileFailed')
    case 'http_get':
      return success ? t('traceDetail.tools.httpGet') : t('traceDetail.tools.httpGetFailed')
    case 'calculator':
      return success ? t('traceDetail.tools.calculator') : t('traceDetail.tools.calculatorFailed')
    case 'local_business_search':
      return success
        ? t('traceDetail.tools.localBusinessSearch')
        : t('traceDetail.tools.localBusinessSearchFailed')
    default:
      return null
  }
}

// A knowledge-base tool's own event carries the retrieval counts
// (`_kb_tool_trace_data`), which no other tool's does, and its `tool` is the
// collection's customer-chosen name (`knowledge_base.py` sets the wrapper's
// __name__ to it). The query itself stays out: what was searched for is the
// agent's working detail, not a step the customer needs narrated.
//
// Success is not a parameter: the adapter builds this shape only on the
// success path, so a failed search has no `hit_count` and never reaches here.
// It falls to the custom-tool wording below, which names the collection too.
function knowledgeBaseTitle(t: Translate, name: string, hits: number): string {
  if (hits === 0) return t('traceDetail.knowledgeBaseEmpty', { name })
  if (hits === 1) return t('traceDetail.knowledgeBaseOne', { name })
  return t('traceDetail.knowledgeBase', { name, count: hits })
}

// Narrates one event for the customer's expanded run view, or returns null
// for an event that view deliberately does not show.
//
// This is the third register in this file, and the one a customer sees when
// they ask for more than the collapsed summary. It exists because the only
// expanded view used to be `EVENT_LABELS` + `renderEventData`, which is
// written for an operator: it names the platform's tool identifiers, times
// each call in milliseconds, counts a tool-calling agent's iterations, and
// reports the memory and grounding machinery -- none of which is the
// customer's to read, and some of which is ours. That register stays exactly
// as it is for the admin trace page (AdminRunDetail.tsx).
//
// `displayNameFor` resolves an agent's technical name to the friendly one the
// wizard gave it (GET /api/pipelines' `agent_display_names`), passing the
// technical name through when a team has none.
export function useDetailedEventLine(displayNameFor: (agentName: string) => string) {
  const { t } = useTranslation()
  return (event: TraceEvent): DetailedEventLine | null => {
    const data = (typeof event.data === 'object' && event.data !== null ? event.data : {}) as Record<
      string,
      unknown
    >
    const agent = displayNameFor(event.agent ?? '')

    switch (event.type) {
      case 'run_queued':
        return { title: t('traceEvents.queued') }
      case 'run_started':
        return { title: t('traceEvents.started') }
      case 'run_completed':
        return { title: t('traceEvents.completed') }
      case 'run_failed':
        return { title: t('traceEvents.failed') }
      case 'run_cancelled':
        return { title: t('traceEvents.cancelled') }
      case 'agent_started':
        return {
          title: t('traceDetail.agentStarted', { agent }),
          detail: (data.role as string | undefined) || undefined,
        }
      case 'agent_completed':
        return { title: t('traceEvents.agentDone', { agent }) }
      case 'delegation_started':
        return {
          title: t('traceDetail.delegated', { from: agent, to: displayNameFor(data.to as string) }),
          detail: (data.task_summary as string | undefined) || undefined,
        }
      case 'delegation_completed':
        // No summary line: the subordinate's answer is working material the
        // manager then acts on, and printing it here says the same thing
        // twice by the time the run's own output lands.
        return {
          title: t('traceDetail.delegationDone', { from: agent, to: displayNameFor(data.to as string) }),
        }
      case 'tool_completed': {
        const tool = data.tool as string | undefined
        if (!tool || tool.startsWith(DELEGATION_TOOL_PREFIX)) return null
        const success = data.success !== false
        if ('hit_count' in data) {
          return { title: knowledgeBaseTitle(t, tool, Number(data.hit_count) || 0) }
        }
        const builtIn = builtInToolTitle(t, tool, success)
        if (builtIn) return { title: builtIn }
        return {
          title: success
            ? t('traceDetail.customTool', { tool })
            : t('traceDetail.customToolFailed', { tool }),
        }
      }
      default:
        // tool_started (its completion says the same thing), agent_progress
        // (an iteration counter), subagent_* (the delegation pair seen from
        // the other side), memory_* and grounding_checked (platform
        // machinery), and the admin-only diagnostic types.
        return null
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
  // Grounding-lite (core/grounding.py): a knowledge-base agent's citations
  // checked against its own searches.
  grounding_checked: '📎 grounding checked',
  // The live milestone (spec 2026-09-05): never persisted, so this row is
  // live-only -- it appears while `useRunTrace` streams a running run and
  // vanishes on reload, unlike every other row here.
  agent_working: '⚡ live update',
  // Admin diagnostic re-runs only (core/trace.py) -- never on a customer run.
  agent_prompt: '📝 prompt',
  model_turn: '💬 model turn',
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
    case 'agent_working': {
      const kind = data.kind === 'subagent' ? 'sub-agent' : 'agent'
      return `${kind} ${data.state as string | undefined}`
    }
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
    case 'agent_prompt': {
      // Sizes only -- the full text is in the raw payload; a whole system
      // prompt on the summary line would bury the timeline.
      const systemPrompt = (data.system_prompt as string | undefined) ?? ''
      const input = (data.input as string | undefined) ?? ''
      return `system prompt ${systemPrompt.length} chars · input ${input.length} chars`
    }
    case 'model_turn': {
      const parts = [`turn ${data.turn as number}`]
      const calls = (data.tool_calls as { name: string }[] | undefined) ?? []
      if (calls.length > 0) parts.push(`calls ${calls.map((c) => c.name).join(', ')}`)
      const content = (data.content as string | undefined) ?? ''
      if (content) parts.push(content.length > 200 ? `${content.slice(0, 200)}…` : content)
      return parts.join(' · ')
    }
    case 'grounding_checked': {
      const searches = (data.searches as number | undefined) ?? 0
      const parts = [
        `${searches} ${searches === 1 ? 'search' : 'searches'}`,
        `${(data.hit_count as number | undefined) ?? 0} passages`,
        `${(data.cited as number | undefined) ?? 0} cited`,
        `${(data.verified as number | undefined) ?? 0} verified`,
      ]
      const unverified = (data.unverified as string[] | undefined) ?? []
      let line = parts.join(' · ')
      if (unverified.length > 0) line += ` — unverified: ${unverified.join(', ')}`
      // grounding_policy (retry/refuse): say when the policy acted. Absent
      // under the default observe policy, so the line is unchanged there.
      if (data.retried === true) line += ' — retried'
      if (data.refused === true) line += ' — answer refused'
      return line
    }
    default:
      return JSON.stringify(data)
  }
}
