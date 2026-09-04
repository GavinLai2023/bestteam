import { useState } from 'react'
import { api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import { useRunTrace } from '../lib/useRunTrace'
import type { DiagnoseRunResult, TraceEvent, UsageRecord } from '../lib/types'
import '../pages/MonitorPage.css' // reuses .event/.event-*/.result styling
import './AdminRunDetail.css'

interface AdminRunDetailProps {
  runId: string
  status: string
  // Set when the run being viewed is itself an admin's diagnostic re-run
  // (see `diagnoseRun`): shows the banner back to the original instead of
  // the Diagnose button.
  diagnosticOfRunId?: string | null
  versionChanged?: boolean
  onDiagnosed?: (result: DiagnoseRunResult) => void
  onOpenRun?: (runId: string) => void
}

const RUN_LEVEL = '(run-level)'

// Event payloads only a diagnostic run carries: a whole system prompt, a
// model turn, a tool's args and full result. Rendered collapsed so one long
// prompt doesn't push the rest of the timeline off-screen.
function isDiagnosticPayload(event: TraceEvent): boolean {
  if (event.type === 'agent_prompt' || event.type === 'model_turn') return true
  if (typeof event.data !== 'object' || event.data === null) return false
  if (event.type === 'tool_started') return 'args' in event.data
  return event.type === 'tool_completed' && 'result' in event.data
}

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
// diagnosing how a pipeline's agents actually behaved, not the customer-
// facing view. Reuses lib/traceEvents.ts's labels/rendering unmodified (no
// redaction changes) and the same lib/useRunTrace.ts fetch/stream hook
// RunDetail uses. "Diagnose this run" starts an admin diagnostic re-run
// (POST /api/runs/{id}/diagnose) whose trace additionally carries prompts,
// model turns and tool args/results -- see ui/backend/CLAUDE.md.
export default function AdminRunDetail({
  runId,
  status,
  diagnosticOfRunId,
  versionChanged,
  onDiagnosed,
  onOpenRun,
}: AdminRunDetailProps) {
  const { events, usage, internalError, error } = useRunTrace(runId, status)
  const [diagnosing, setDiagnosing] = useState(false)
  const [diagnoseError, setDiagnoseError] = useState<string | null>(null)
  const groups = groupByAgent(events)
  const usageByAgentMap = usageByAgent(usage)
  const finalEventType = events.find((e) => TERMINAL_TYPES.includes(e.type))?.type
  const finalEvent = events.find((e) => e.type === finalEventType)
  const canDiagnose = !diagnosticOfRunId && status !== 'running'

  async function diagnose() {
    setDiagnosing(true)
    setDiagnoseError(null)
    try {
      const result = await api.diagnoseRun(runId)
      onDiagnosed?.(result)
    } catch (e) {
      setDiagnoseError((e as Error).message)
    } finally {
      setDiagnosing(false)
    }
  }

  return (
    <div className="admin-run-detail">
      {error && <p className="banner banner-error">{error}</p>}
      {internalError && (
        // Served to a platform admin only. The customer's `run_failed` event
        // says a fixed sentence, because a provider's own text can name the
        // model, the provider and the account's billing state (runtime.py).
        <section className="admin-run-detail-internal-error">
          <h3>Why it failed</h3>
          <pre>{internalError}</pre>
        </section>
      )}
      {diagnosticOfRunId ? (
        <div className="banner banner-info admin-run-detail-diagnostic">
          <p>
            Diagnostic re-run of run {diagnosticOfRunId}: this trace also shows each agent's prompt, every model
            turn and the tool arguments/results. Memory context is not reproduced.
            {versionChanged && ' The team was redeployed after the original run, so this diagnoses the current version.'}
          </p>
          {onOpenRun && (
            <button type="button" onClick={() => onOpenRun(diagnosticOfRunId)}>
              Open original run
            </button>
          )}
        </div>
      ) : (
        canDiagnose && (
          <div className="admin-run-detail-actions">
            <button type="button" onClick={diagnose} disabled={diagnosing}>
              {diagnosing ? 'Starting…' : 'Diagnose this run'}
            </button>
            <span className="hint">
              Re-runs the same input against the team as currently deployed, recording prompts, model turns and tool
              results. Spends against this organisation like any other run.
            </span>
          </div>
        )
      )}
      {diagnoseError && <p className="banner banner-error">{diagnoseError}</p>}
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
                  {event.data != null &&
                    (isDiagnosticPayload(event) ? (
                      <details className="admin-run-detail-raw-details">
                        <summary>Full payload</summary>
                        <pre className="admin-run-detail-raw">{JSON.stringify(event.data, null, 2)}</pre>
                      </details>
                    ) : (
                      <pre className="admin-run-detail-raw">{JSON.stringify(event.data, null, 2)}</pre>
                    ))}
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
