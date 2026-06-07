import { useEffect, useRef, useState } from 'react'
import './App.css'

const API_BASE = 'http://127.0.0.1:8000'
const WS_BASE = 'ws://127.0.0.1:8000'

const EVENT_LABELS = {
  run_started: '▶ started',
  agent_completed: '✓ agent done',
  run_completed: '● completed',
  run_failed: '✕ failed',
}

function App() {
  const [workflows, setWorkflows] = useState([])
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [events, setEvents] = useState([])
  const [status, setStatus] = useState('idle') // idle | running | completed | failed | unreachable
  const wsRef = useRef(null)

  useEffect(() => {
    fetch(`${API_BASE}/api/workflows`)
      .then((r) => r.json())
      .then((data) => {
        setWorkflows(data.workflows)
        if (data.workflows.length) setSelected(data.workflows[0])
      })
      .catch(() => setStatus('unreachable'))
  }, [])

  // Close any open socket when the component unmounts.
  useEffect(() => () => wsRef.current?.close(), [])

  const startRun = async () => {
    if (!selected || !input.trim() || status === 'running') return

    setEvents([])
    setStatus('running')
    wsRef.current?.close()

    const res = await fetch(`${API_BASE}/api/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workflow: selected, input }),
    })
    const { run_id: runId } = await res.json()

    const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream`)
    wsRef.current = ws
    ws.onmessage = (message) => {
      const event = JSON.parse(message.data)
      setEvents((prev) => [...prev, event])
      if (event.type === 'run_completed') setStatus('completed')
      if (event.type === 'run_failed') setStatus('failed')
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

        <button onClick={startRun} disabled={status === 'running' || !selected}>
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

export default App
