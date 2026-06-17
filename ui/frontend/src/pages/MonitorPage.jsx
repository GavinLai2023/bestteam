import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { API_BASE, WS_BASE, TOKEN_KEY, api } from '../lib/api'
import './MonitorPage.css'

const EVENT_LABELS = {
  run_started: '▶ started',
  agent_completed: '✓ agent done',
  run_completed: '● completed',
  run_failed: '✕ failed',
}

function MonitorPage() {
  const [searchParams] = useSearchParams()
  const [workflows, setWorkflows] = useState([])
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [events, setEvents] = useState([])
  const [status, setStatus] = useState('idle') // idle | running | completed | failed | unreachable
  const wsRef = useRef(null)

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
      .catch(() => setStatus('unreachable'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Close any open socket when the component unmounts.
  useEffect(() => () => wsRef.current?.close(), [])

  const startRun = async () => {
    if (!selected || !input.trim() || status === 'running') return

    setEvents([])
    setStatus('running')
    wsRef.current?.close()

    const { run_id: runId } = await api.createRun(selected, input)

    const token = localStorage.getItem(TOKEN_KEY)
    const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?token=${encodeURIComponent(token ?? '')}`)
    wsRef.current = ws
    ws.onmessage = (message) => {
      const event = JSON.parse(message.data)
      setEvents((prev) => [...prev, event])
      if (event.type === 'run_completed') setStatus('completed')
      if (event.type === 'run_failed') setStatus('failed')
    }
    ws.onerror = () => {
      setStatus('unreachable')
    }
    ws.onclose = () => {
      // onclose always fires, including after a clean run_completed/run_failed
      // that onmessage already handled -- only downgrade to 'unreachable' if
      // the socket closed while still running.
      setStatus((current) => (current === 'running' ? 'unreachable' : current))
    }
  }

  const finalEvent = events.find((e) => e.type === 'run_completed' || e.type === 'run_failed')

  return (
    <div className="dashboard">
      <header>
        <h1>bestteam · runtime monitor</h1>
        <p>Pick a workflow, give it an input, and watch agents hand off live.</p>
      </header>

      {status === 'unreachable' && (
        <p className="banner banner-error">
          Can't reach the backend at {API_BASE}. Is `uvicorn ui.backend.main:app` running?
        </p>
      )}

      <section className="controls">
        <label>
          Workflow
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
            placeholder="e.g. Review this Python function for bugs..."
            rows={3}
          />
        </label>

        <button onClick={startRun} disabled={status === 'running' || !selected || !input.trim()}>
          {status === 'running' ? 'Running…' : 'Run'}
        </button>
      </section>

      <section className="trace">
        <h2>Live trace</h2>
        {events.length === 0 ? (
          <p className="hint">No run yet — pick a workflow and hit Run.</p>
        ) : (
          <ul>
            {events.map((event, i) => (
              <li key={i} className={`event event-${event.type}`}>
                <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
                {event.agent && <span className="event-agent">{event.agent}</span>}
                <p className="event-data">{event.data}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h2>{finalEvent.type === 'run_completed' ? 'Final output' : 'Run failed'}</h2>
          <p>{finalEvent.data}</p>
        </section>
      )}
    </div>
  )
}

export default MonitorPage
