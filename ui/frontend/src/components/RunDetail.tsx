import { useEffect, useRef, useState } from 'react'
import { WS_BASE, api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import type { AutomationResult, TraceEvent } from '../lib/types'
import '../pages/MonitorPage.css' // reuses .event/.event-*/.result styling

interface RunDetailProps {
  runId: string
  status: string
  autonomous: boolean
  onRetried?: (newRunId: string) => void
}

type RetryState = 'idle' | 'retrying' | 'error'

// A run's event timeline, for the Activity page's Runs tab. A `running` run
// streams live over the same WebSocket MonitorPage uses; anything else reads
// its persisted trace via GET /api/runs/{id}/trace -- no live/historical
// merge, per the read endpoint's design (see docs/superpowers/specs).
export default function RunDetail({ runId, status, autonomous, onRetried }: RunDetailProps) {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [automationResults, setAutomationResults] = useState<AutomationResult[]>([])
  const [retryState, setRetryState] = useState<RetryState>('idle')
  const [retryError, setRetryError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  // Property Maintenance Inbox: this run's structured results, if any (most
  // runs have none -- only autonomous email-triggered runs whose output
  // parsed as one of these envelopes do). A no-op fetch/render for every
  // other kind of run. Refetches on the terminal event too: normalize_run_result
  // only runs server-side after run_completed/run_failed, so opening a still-
  // running run's detail would otherwise leave this section empty until the
  // whole component remounts (Codex review finding).
  const finalEventType = events.find((e) => TERMINAL_TYPES.includes(e.type))?.type
  useEffect(() => {
    let ignore = false
    api
      .listAutomationResults({ run_id: runId })
      .then((data) => {
        if (!ignore) setAutomationResults(data.results)
      })
      .catch(() => {})
    return () => {
      ignore = true
    }
  }, [runId, finalEventType])

  const retry = async () => {
    setRetryState('retrying')
    setRetryError(null)
    try {
      const { run_id: newRunId } = await api.retryRun(runId)
      setRetryState('idle')
      onRetried?.(newRunId)
    } catch (e) {
      setRetryState('error')
      setRetryError((e as Error).message)
    }
  }

  // Callers key this component by runId (see ActivityPage) so switching to a
  // different run remounts it -- a fresh `events`/`error` state -- rather
  // than needing to reset them here.
  useEffect(() => {
    if (status === 'running') {
      let cancelled = false
      ;(async () => {
        try {
          const { ticket } = await api.createWsTicket()
          if (cancelled) return
          const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
          wsRef.current = ws
          ws.onmessage = (message: MessageEvent<string>) => {
            const event = JSON.parse(message.data) as TraceEvent
            setEvents((prev) => [...prev, event])
          }
          ws.onerror = () => setError("Couldn't stream this run.")
        } catch (e) {
          if (!cancelled) setError((e as Error).message)
        }
      })()
      return () => {
        cancelled = true
        wsRef.current?.close()
      }
    }

    let ignore = false
    api
      .getRunTrace(runId)
      .then((data) => {
        if (!ignore) setEvents(data.events)
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [runId, status])

  const finalEvent = events.find((e) => e.type === finalEventType)

  return (
    <div className="run-detail">
      {error && <p className="banner banner-error">{error}</p>}
      {events.length === 0 && !error ? (
        <p className="hint">{status === 'running' ? 'Waiting for events…' : 'No trace recorded for this run.'}</p>
      ) : (
        <ul className="run-detail-events">
          {events.map((event, i) => (
            <li key={i} className={`event event-${event.type}`}>
              <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
              {event.agent && <span className="event-agent">{event.agent}</span>}
              <p className="event-data">{renderEventData(event)}</p>
            </li>
          ))}
        </ul>
      )}
      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h3>{RESULT_LABELS[finalEvent.type]}</h3>
          <p>{finalEvent.data as string}</p>
        </section>
      )}
      {automationResults.length > 0 && (
        <section className="automation-results">
          <h3>Automation results</h3>
          <ul className="automation-results-list">
            {automationResults.map((result) => {
              const payload = result.payload || {}
              return (
                <li key={result.id}>
                  <span className="status-badge">{result.status}</span>
                  {payload.priority && <span className="status-badge">{payload.priority}</span>}
                  <p>{payload.summary || '(no summary)'}</p>
                  <p className="hint">{payload.extracted?.property_address || 'Address not identified'}</p>
                  {payload.classification && (
                    <p className="hint">
                      {payload.classification}
                      {payload.category ? ` · ${payload.category}` : ''}
                    </p>
                  )}
                  {(payload.missing_information?.length ?? 0) > 0 && (
                    <p className="hint">Missing: {payload.missing_information!.join(', ')}</p>
                  )}
                  {(payload.risk_reasons?.length ?? 0) > 0 && (
                    <p className="hint">Risk: {payload.risk_reasons!.join(', ')}</p>
                  )}
                  {payload.human_reason && <p className="hint">Why: {payload.human_reason}</p>}
                  <p className="hint">{payload.action?.draft_created ? 'Draft created' : 'No draft created'}</p>
                </li>
              )
            })}
          </ul>
        </section>
      )}
      {/* finalEventType covers a run that fails WHILE this panel is open: ActivityPage's
          selectedRun.status is only set at click time, so `status` alone stays stale
          at 'running' until the panel is closed and reopened (Codex review finding).
          `autonomous` gates out a manual run's failure -- POST /api/runs/{id}/retry
          only accepts a run with a recorded trigger_context, so showing this for a
          manual run always 400s (Codex review finding). */}
      {(status === 'failed' || finalEventType === 'run_failed') && autonomous && (
        <section className="run-detail-retry">
          <button type="button" onClick={retry} disabled={retryState === 'retrying'}>
            {retryState === 'retrying' ? 'Retrying…' : 'Retry'}
          </button>
          {retryState === 'error' && <p className="banner banner-error">{retryError}</p>}
        </section>
      )}
    </div>
  )
}
