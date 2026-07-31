import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { API_BASE, WS_BASE, api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import './MonitorPage.css'

const NON_PROGRESS_TYPES = ['run_queued', 'run_started']
const STALE_HINT_SECONDS = 20

function MonitorPage() {
  const [searchParams] = useSearchParams()
  const [workflows, setWorkflows] = useState([])
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [events, setEvents] = useState([])
  const [status, setStatus] = useState('idle') // idle | running | completed | failed | cancelled | unreachable
  const [error, setError] = useState(null)
  const [connectionStatus, setConnectionStatus] = useState('idle') // idle | connecting | connected | disconnected
  const [cancelling, setCancelling] = useState(false)
  const [hasRunId, setHasRunId] = useState(false)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [secondsSinceLastEvent, setSecondsSinceLastEvent] = useState(0)
  const wsRef = useRef(null)
  const runIdRef = useRef(null)
  const runStartedAtRef = useRef(null)
  const lastEventAtRef = useRef(null)

  useEffect(() => {
    api.listWorkflows()
      .then((data) => {
        setWorkflows(data.workflows)
        const preferred = searchParams.get('workflow')
        if (preferred && data.workflows.includes(preferred)) {
          setSelected(preferred)
        } else if (data.workflows.length) {
          setSelected(data.workflows[0])
        }
      })
      .catch((err) => {
        // A rejection with an HTTP status means the backend answered (e.g. a
        // 403 for a platform operator with no org) -- it is reachable, so show
        // the real reason rather than the misleading "is uvicorn running?".
        // Only a statusless failure (fetch rejected) is a true unreachable.
        if (err?.status !== undefined) {
          setError(err.message)
        } else {
          setStatus('unreachable')
        }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data)
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
      setError(e.message)
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
      setError(e.message)
      setCancelling(false)
    }
  }

  const finalEvent = events.find((e) => TERMINAL_TYPES.includes(e.type))
  const isWaitingForFirstProgress = status === 'running' && !events.some((e) => !NON_PROGRESS_TYPES.includes(e.type))

  return (
    <div className="dashboard">
      <header>
        <h1>Run a team</h1>
        <p>Choose a team, give it a task, and follow its progress.</p>
      </header>

      {status === 'unreachable' && (
        <p className="banner banner-error">
          Can't reach the backend at {API_BASE}. Is `uvicorn ui.backend.main:app` running?
        </p>
      )}

      {error && <p className="banner banner-error">{error}</p>}

      <section className="controls">
        <label>
          Team
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            {workflows.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Input
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Describe what you would like this team to do..."
            rows={3}
          />
        </label>

        <div className="controls-actions">
          <button onClick={startRun} disabled={status === 'running' || !selected || !input.trim()}>
            {status === 'running' ? 'Running…' : 'Run'}
          </button>
          {status === 'running' && hasRunId && (
            <button type="button" className="cancel-button" onClick={cancelRun} disabled={cancelling}>
              {cancelling ? 'Stopping…' : 'Stop'}
            </button>
          )}
        </div>
      </section>

      {status === 'running' && (
        <section className="run-status">
          <span className="run-status-spinner" aria-hidden="true" />
          <span>Running for {elapsedSeconds}s</span>
          <span className="run-status-connection">
            {connectionStatus === 'connected' && 'Connected'}
            {connectionStatus === 'connecting' && 'Connecting…'}
            {connectionStatus === 'disconnected' && 'Disconnected'}
          </span>
          {isWaitingForFirstProgress && <p className="hint">Waiting for the agent/model…</p>}
          {secondsSinceLastEvent !== null && secondsSinceLastEvent >= STALE_HINT_SECONDS && (
            <p className="banner run-status-stale">
              No update for {secondsSinceLastEvent}s — still working, this can take a while for longer tasks.
            </p>
          )}
        </section>
      )}

      <section className="trace">
        <h2>Live trace</h2>
        {events.length === 0 ? (
          <p className="hint">No run yet — pick a team and hit Run.</p>
        ) : (
          <ul>
            {events.map((event, i) => (
              <li key={i} className={`event event-${event.type}`}>
                <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
                {event.agent && <span className="event-agent">{event.agent}</span>}
                <p className="event-data">{renderEventData(event)}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h2>{RESULT_LABELS[finalEvent.type]}</h2>
          <p>{finalEvent.data}</p>
        </section>
      )}
    </div>
  )
}

export default MonitorPage
