import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { API_BASE, WS_BASE, api } from '../lib/api'
import ShareLinksPanel from '../components/ShareLinksPanel'
import {
  EVENT_LABELS,
  FRIENDLY_EVENT_TYPES,
  RESULT_LABELS,
  TERMINAL_TYPES,
  renderEventData,
  useFriendlyEventTitle,
} from '../lib/traceEvents'
import type { TraceEvent } from '../lib/types'
import './MonitorPage.css'

const NON_PROGRESS_TYPES = ['run_queued', 'run_started']
const STALE_HINT_SECONDS = 20

type Status = 'idle' | 'running' | 'completed' | 'failed' | 'cancelled' | 'unreachable'
type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'disconnected'

function MonitorPage() {
  const { t } = useTranslation()
  const [searchParams] = useSearchParams()
  const [pipelines, setPipelines] = useState<string[]>([])
  // Technical name -> the team's own friendly name, so the picker labels a
  // team the way every other surface does. The list beside it already showed
  // `team_display_name` while this select showed the raw slug, so one screen
  // called one team two things (audit finding F4).
  const [displayNames, setDisplayNames] = useState<Record<string, string>>({})
  // A YAML-only demo pipeline has no PipelineRecord.id, so there's nothing to
  // hang a share link off -- the Share button below only renders for a team
  // that has one.
  const [pipelineIds, setPipelineIds] = useState<Record<string, number>>({})
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('idle')
  const [cancelling, setCancelling] = useState(false)
  const [hasRunId, setHasRunId] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [secondsSinceLastEvent, setSecondsSinceLastEvent] = useState(0)
  const [traceExpanded, setTraceExpanded] = useState(false)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const wsRef = useRef<WebSocket | null>(null)
  const runIdRef = useRef<string | null>(null)
  const runStartedAtRef = useRef<number | null>(null)
  const lastEventAtRef = useRef<number | null>(null)

  const loadPipelines = useCallback(() => {
    setError(null)
    api.listPipelines()
      .then((data) => {
        setPipelines(data.pipelines)
        setDisplayNames(data.display_names ?? {})
        setPipelineIds(data.pipeline_ids ?? {})
        // Clear a previous unreachable banner, but never stomp on a run that
        // is currently in flight or has already reached a terminal state.
        setStatus((current) => (current === 'unreachable' ? 'idle' : current))
        const preferred = searchParams.get('pipeline')
        if (preferred && data.pipelines.includes(preferred)) {
          setSelected(preferred)
        } else if (data.pipelines.length) {
          setSelected(data.pipelines[0])
        }
      })
      .catch((err: { status?: number; message: string }) => {
        // A rejection with an HTTP status means the backend answered (e.g. a
        // 403 for a platform operator with no org) -- it is reachable, so show
        // the real reason rather than a generic connection message. Only a
        // statusless failure (fetch rejected) is a true unreachable.
        if (err?.status !== undefined) {
          // Starlette's own default body for an unmatched route is literally
          // `{"detail": "Not Found"}` -- no route in this app ever writes
          // that string deliberately. It carries nothing a customer can act
          // on, so it goes to the console (like the unreachable case above)
          // rather than onto a banner that would contradict the calm,
          // correct "No teams yet" hint sitting right below it.
          if (err.message !== 'Not Found') setError(err.message)
          else console.error('bestteam: unexpected 404 loading pipelines', err)
          // The backend answered, so a banner left over from an earlier
          // network failure is now saying something untrue -- clear it on
          // this path too, under the same guard the success path uses.
          setStatus((current) => (current === 'unreachable' ? 'idle' : current))
        } else {
          // Which host and what should be listening on it is an operator's
          // problem, not the customer's -- it belongs in the console, not on
          // the page (see the banner below).
          console.error(`bestteam: cannot reach the backend at ${API_BASE}`, err)
          setStatus('unreachable')
        }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch; loadPipelines is also the retry handler
    loadPipelines()
  }, [loadPipelines])

  // Close any open socket when the component unmounts.
  useEffect(() => () => wsRef.current?.close(), [])

  // Ticks once a second while a run is in flight, driving the elapsed-time
  // and time-since-last-event displays below. Timestamps live in refs (not
  // read during render) and Date.now() is only called from this timer
  // callback, never from render itself.
  useEffect(() => {
    if (status !== 'running') return undefined
    const id = setInterval(() => {
      if (runStartedAtRef.current) {
        setElapsedSeconds(Math.max(0, Math.floor((Date.now() - runStartedAtRef.current) / 1000)))
      }
      if (lastEventAtRef.current) {
        setSecondsSinceLastEvent(Math.max(0, Math.floor((Date.now() - lastEventAtRef.current) / 1000)))
      }
    }, 1000)
    return () => clearInterval(id)
  }, [status])

  const startRun = async () => {
    if (!selected || !input.trim() || status === 'running') return

    setEvents([])
    setStatus('running')
    setError(null)
    setConnectionStatus('connecting')
    setCancelling(false)
    setElapsedSeconds(0)
    setSecondsSinceLastEvent(0)
    setHasRunId(false)
    setTraceExpanded(false)
    setCopyState('idle')
    wsRef.current?.close()
    // Clear immediately -- otherwise a Stop click in the window before the
    // new run id arrives below would silently target the previous run.
    runIdRef.current = null
    runStartedAtRef.current = Date.now()
    lastEventAtRef.current = Date.now()

    try {
      const { run_id: runId } = await api.createRun(selected, input)
      runIdRef.current = runId
      setHasRunId(true)

      const { ticket } = await api.createWsTicket()
      const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
      wsRef.current = ws
      ws.onopen = () => setConnectionStatus('connected')
      ws.onmessage = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as TraceEvent
        lastEventAtRef.current = Date.now()
        setEvents((prev) => [...prev, event])
        if (event.type === 'run_completed') setStatus('completed')
        if (event.type === 'run_failed') setStatus('failed')
        if (event.type === 'run_cancelled') setStatus('cancelled')
      }
      ws.onerror = () => {
        setConnectionStatus('disconnected')
        setStatus('unreachable')
      }
      ws.onclose = () => {
        // onclose always fires, including after a clean terminal event that
        // onmessage already handled -- only downgrade to 'unreachable' if
        // the socket closed while still running.
        setConnectionStatus('disconnected')
        setStatus((current) => (current === 'running' ? 'unreachable' : current))
      }
    } catch (e) {
      setError((e as Error).message)
      setStatus('idle')
      setConnectionStatus('idle')
    }
  }

  const cancelRun = async () => {
    if (!runIdRef.current || cancelling) return
    setCancelling(true)
    try {
      await api.cancelRun(runIdRef.current)
    } catch (e) {
      setError((e as Error).message)
      setCancelling(false)
    }
  }

  const finalEvent = events.find((e) => TERMINAL_TYPES.includes(e.type))
  const isWaitingForFirstProgress = status === 'running' && !events.some((e) => !NON_PROGRESS_TYPES.includes(e.type))

  // Nothing constrains a team's friendly name to be unique, so two deployed
  // teams can carry the same one. Showing both as identical options would ask
  // the customer to pick blind; the technical name is appended to the
  // colliding ones only, so the common case stays clean.
  const teamLabel = (name: string) => {
    const display = displayNames[name]
    if (!display) return name
    const collides = pipelines.some((other) => other !== name && displayNames[other] === display)
    return collides ? t('run.teamLabelAmbiguous', { display, name }) : display
  }
  // Resolves an agent's technical name for the friendly feed. `agent` here is
  // whatever the engine emitted; there is no specification on this page to map
  // it against, so it passes through -- the friendly view's value is the
  // sentence around it, not the name itself.
  const friendlyTitle = useFriendlyEventTitle((agentName) => agentName)
  const friendlyEvents = events.filter((e) => FRIENDLY_EVENT_TYPES.includes(e.type))

  return (
    <div className="dashboard">
      <header>
        <h1>{t('run.title')}</h1>
        <p>{t('run.subtitle')}</p>
      </header>

      {status === 'unreachable' && (
        <div className="banner banner-error">
          {t('run.unreachable')}
          <div className="banner-actions">
            <button type="button" className="btn-secondary" onClick={loadPipelines}>
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      {error && <p className="banner banner-error">{error}</p>}

      <section className="controls">
        <label>
          {t('run.teamLabel')}
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            {pipelines.map((name) => (
              <option key={name} value={name}>
                {teamLabel(name)}
              </option>
            ))}
          </select>
        </label>
        {status !== 'unreachable' && pipelines.length === 0 && (
          <p className="hint">{t('run.noTeams')}</p>
        )}
        {pipelineIds[selected] != null && <ShareLinksPanel pipelineId={pipelineIds[selected]} />}

        <label>
          {t('run.taskLabel')}
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t('run.taskPlaceholder')}
            rows={3}
          />
        </label>

        <div className="controls-actions">
          <button onClick={startRun} disabled={status === 'running' || !selected || !input.trim()}>
            {status === 'running' ? t('run.running') : t('run.start')}
          </button>
          {status === 'running' && hasRunId && (
            <button type="button" className="cancel-button" onClick={cancelRun} disabled={cancelling}>
              {cancelling ? t('run.stopping') : t('run.stop')}
            </button>
          )}
        </div>
      </section>

      {status === 'running' && (
        <section className="run-status">
          <span className="run-status-spinner" aria-hidden="true" />
          <span>{t('run.runningFor', { seconds: elapsedSeconds })}</span>
          <span className="run-status-connection">
            {connectionStatus === 'connected' && t('run.connected')}
            {connectionStatus === 'connecting' && t('run.connecting')}
            {connectionStatus === 'disconnected' && t('run.disconnected')}
          </span>
          {isWaitingForFirstProgress && <p className="hint">{t('run.waitingFirstStep')}</p>}
          {secondsSinceLastEvent !== null && secondsSinceLastEvent >= STALE_HINT_SECONDS && (
            <p className="banner run-status-stale">
              {t('run.stale', { seconds: secondsSinceLastEvent })}
            </p>
          )}
        </section>
      )}

      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h2>{RESULT_LABELS[finalEvent.type]}</h2>
          {/* Safe: terminal-event `data` is guaranteed to be a string by the backend's
              event-emission contract, unlike PreviewPage.tsx's more defensive handling. */}
          <p>{finalEvent.data as string}</p>
          <div className="result-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                if (!navigator.clipboard) {
                  setCopyState('failed')
                  return
                }
                try {
                  await navigator.clipboard.writeText(finalEvent.data as string)
                  setCopyState('copied')
                } catch {
                  setCopyState('failed')
                }
              }}
            >
              {copyState === 'copied'
                ? t('common.copied')
                : copyState === 'failed'
                  ? t('common.copyFailed')
                  : t('common.copy')}
            </button>
            <button type="button" onClick={startRun}>
              {t('run.runAgain')}
            </button>
          </div>
        </section>
      )}

      {/* Two registers, and which one is the default is the point: the wizard
          already narrated this same stream in plain language while this page
          showed `✓ agent done` and raw agent names. The friendly feed is now
          what a customer sees, and the technical trace is one click away for
          whoever wants it (audit finding F8). */}
      <section className="trace">
        <div className="trace-header">
          <h2>{t('run.progress')}</h2>
          <button type="button" className="btn-link" onClick={() => setTraceExpanded((x) => !x)}>
            {traceExpanded ? t('run.hideTechnical') : t('run.showTechnical')}
          </button>
        </div>
        {events.length === 0 ? (
          <p className="hint">{t('run.noRunYet')}</p>
        ) : traceExpanded ? (
          <ul>
            {events.map((event, i) => (
              <li key={i} className={`event event-${event.type}`}>
                <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
                {event.agent && <span className="event-agent">{event.agent}</span>}
                <p className="event-data">{renderEventData(event)}</p>
              </li>
            ))}
          </ul>
        ) : (
          <ul>
            {friendlyEvents.map((event, i) => (
              <li key={i} className={`event event-${event.type}`}>
                <span className="event-type">{friendlyTitle(event)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

export default MonitorPage
