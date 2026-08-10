import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import { useRunTrace } from '../lib/useRunTrace'
import type { TraceEvent, UsageRecord } from '../lib/types'
import '../pages/MonitorPage.css' // reuses .event/.event-*/.result styling
import './AdminRunDetail.css'

interface AdminRunDetailProps {
  runId: string
  status: string
}

const RUN_LEVEL = '(run-level)'

function groupByAgent(events: TraceEvent[]): [string, TraceEvent[]][] {
  const groups = new Map<string, TraceEvent[]>()
  for (const event of events) {
    const key = event.agent ?? RUN_LEVEL
    const list = groups.get(key)
    if (list) list.push(event)
    else groups.set(key, [event])
  }
  return Array.from(groups.entries())
}

function usageByAgent(usage: UsageRecord[]): Map<string, UsageRecord[]> {
  const byAgent = new Map<string, UsageRecord[]>()
  for (const record of usage) {
    const key = record.agent ?? RUN_LEVEL
    const list = byAgent.get(key)
    if (list) list.push(record)
    else byAgent.set(key, [record])
  }
  return byAgent
}

// The admin superset of components/RunDetail.tsx: events grouped by agent,
// the full raw event `data` payload alongside the same friendly summary
// customers see, and per-agent token/cost usage -- for a platform admin
// diagnosing how a workflow's agents actually behaved, not the customer-
// facing view. Reuses lib/traceEvents.ts's labels/rendering unmodified (no
// redaction changes) and the same lib/useRunTrace.ts fetch/stream hook
// RunDetail uses.
export default function AdminRunDetail({ runId, status }: AdminRunDetailProps) {
  const { events, usage, error } = useRunTrace(runId, status)
  const groups = groupByAgent(events)
  const usageByAgentMap = usageByAgent(usage)
  const finalEventType = events.find((e) => TERMINAL_TYPES.includes(e.type))?.type
  const finalEvent = events.find((e) => e.type === finalEventType)

  return (
    <div className="admin-run-detail">
      {error && <p className="banner banner-error">{error}</p>}
      {events.length === 0 && !error ? (
        <p className="hint">{status === 'running' ? 'Waiting for events…' : 'No trace recorded for this run.'}</p>
      ) : (
        groups.map(([agent, agentEvents]) => (
          <section key={agent} className="admin-run-detail-group">
            <h3>{agent === RUN_LEVEL ? 'Run' : agent}</h3>
            {usageByAgentMap.get(agent) && (
              <ul className="admin-run-detail-usage">
                {usageByAgentMap.get(agent)!.map((record, i) => (
                  <li key={i}>
                    <span className="status-badge">{record.model ?? 'unknown model'}</span>{' '}
                    {record.input_tokens} in / {record.output_tokens} out tokens
                    {record.cost_estimate != null ? ` · $${record.cost_estimate.toFixed(4)}` : ''}
                  </li>
                ))}
              </ul>
            )}
            <ul className="run-detail-events">
              {agentEvents.map((event, i) => (
                <li key={i} className={`event event-${event.type}`}>
                  <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
                  <p className="event-data">{renderEventData(event)}</p>
                  {event.data != null && (
                    <pre className="admin-run-detail-raw">{JSON.stringify(event.data, null, 2)}</pre>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h3>{RESULT_LABELS[finalEvent.type]}</h3>
          <p>{finalEvent.data as string}</p>
        </section>
      )}
    </div>
  )
}
