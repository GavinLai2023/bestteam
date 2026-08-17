import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import { useRunTrace } from '../lib/useRunTrace'
import type { AutomationResult } from '../lib/types'
import '../pages/MonitorPage.css' // reuses .event/.event-*/.result styling

interface RunDetailProps {
  runId: string
  status: string
  autonomous: boolean
  onRetried?: (newRunId: string) => void
}

type RetryState = 'idle' | 'retrying' | 'error'

// A run's event timeline, for the Activity page's Runs tab. See
// lib/useRunTrace.ts for the live-WS-vs-historical-fetch mechanics (shared
// with the admin Trace page's AdminRunDetail).
export default function RunDetail({ runId, status, autonomous, onRetried }: RunDetailProps) {
  const { events, contentPurgedAt, error } = useRunTrace(runId, status)
  const [automationResults, setAutomationResults] = useState<AutomationResult[]>([])
  const [retryState, setRetryState] = useState<RetryState>('idle')
  const [retryError, setRetryError] = useState<string | null>(null)
  const [purgedAt, setPurgedAt] = useState<string | null>(null)
  const [purging, setPurging] = useState(false)
  const [purgeError, setPurgeError] = useState<string | null>(null)

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

  const purge = async () => {
    if (!window.confirm("Remove this run's content? The message text, our drafted reply and the step-by-step trace go; what it cost and when it ran stay. This cannot be undone.")) return
    setPurging(true)
    setPurgeError(null)
    try {
      await api.purgeRun(runId)
      // Stamped locally rather than refetched: the trace is now empty by
      // definition, so there is nothing left to fetch.
      setPurgedAt(new Date().toISOString())
    } catch (e) {
      setPurgeError((e as Error).message)
    } finally {
      setPurging(false)
    }
  }

  const finalEvent = events.find((e) => e.type === finalEventType)
  // Already purged when it loaded, or purged from this panel just now.
  const purged = purgedAt ?? contentPurgedAt
  // A `running` run's worker is still writing trace events, so the API
  // refuses it (409) -- don't offer a button that can only fail. `status` is
  // set once at click time by ActivityPage, so a run that finished while this
  // panel stayed open needs its own terminal event as the second signal, the
  // same way Retry does below.
  const terminal = status !== 'running' || finalEventType !== undefined

  return (
    <div className="run-detail">
      {error && <p className="banner banner-error">{error}</p>}
      {purged ? (
        <p className="hint">
          The content of this run was removed on {formatDateTime(purged)} by your
          data retention settings. What it cost and when it ran are still on
          record.
        </p>
      ) : events.length === 0 && !error ? (
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
          {/* Safe: terminal-event `data` is guaranteed to be a string by the backend's
              event-emission contract, unlike PreviewPage.tsx's more defensive handling. */}
          <p>{finalEvent.data as string}</p>
        </section>
      )}
      {automationResults.length > 0 && (
        <section className="automation-results">
          <h3>Automation results</h3>
          <ul className="automation-results-list">
            {automationResults.map((result) => {
              const payload = result.payload || {}
              // A retention cleanup empties the payload and keeps the row --
              // its status and source_key are what stop a retry re-drafting
              // the same message. Say so rather than render blank fields.
              if (Object.keys(payload).length === 0) {
                return (
                  <li key={result.id}>
                    <span className="status-badge">{result.status}</span>
                    <p className="hint">Content removed.</p>
                  </li>
                )
              }
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
      {terminal && !purged && (
        <section className="run-detail-purge">
          <button type="button" onClick={() => void purge()} disabled={purging}>
            {purging ? 'Deleting…' : "Delete this run's content"}
          </button>
          {purgeError && <p className="banner banner-error">{purgeError}</p>}
        </section>
      )}
    </div>
  )
}
