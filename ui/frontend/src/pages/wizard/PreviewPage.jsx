import { useEffect, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import TeamFlow from '../../components/TeamFlow'
import { WS_BASE, TOKEN_KEY, api } from '../../lib/api'

export default function PreviewPage() {
  const { session, loading, sessionId } = useOutletContext()
  const navigate = useNavigate()

  const [input, setInput] = useState('')
  const [events, setEvents] = useState([])
  const [status, setStatus] = useState('idle') // idle | running | completed | failed
  const [error, setError] = useState(null)
  const wsRef = useRef(null)

  useEffect(() => () => wsRef.current?.close(), [])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Meet your team</h2>
        <p className="subtitle">We need a bit more information first.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json
  const agentsByName = Object.fromEntries((spec.agents ?? []).map((a) => [a.name, a]))

  const friendlyName = (agentName) => {
    const agent = agentsByName[agentName]
    return agent?.display_name || agentName
  }

  const titleFor = (event) => {
    switch (event.type) {
      case 'run_started':
        return 'Your team got started'
      case 'agent_completed':
        return `${friendlyName(event.agent)} finished their part`
      case 'run_completed':
        return 'All done!'
      case 'run_failed':
        return 'Something went wrong'
      default:
        return event.type
    }
  }

  const run = async () => {
    if (!input.trim() || status === 'running') return
    setEvents([])
    setStatus('running')
    setError(null)
    wsRef.current?.close()

    try {
      const { run_id: runId } = await api.createTestRun(sessionId, input.trim())

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
        setStatus('failed')
        setError('Lost connection to the backend while your team was working. Please try again.')
      }
      ws.onclose = () => {
        setStatus((current) => {
          if (current === 'running') {
            setError('Lost connection to the backend while your team was working. Please try again.')
            return 'failed'
          }
          return current
        })
      }
    } catch (e) {
      setError(e.message)
      setStatus('idle')
    }
  }

  return (
    <div className="wizard-card">
      <h2>Meet your team</h2>
      <p className="subtitle">
        Here's the team we've put together for "{spec.name}". Try giving them a real task below.
      </p>

      {error && <p className="banner banner-error">{error}</p>}

      <TeamFlow specification={spec} />

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <h3>Try them out</h3>
      <div className="field">
        <label htmlFor="test-input">A real task or message for your team</label>
        <textarea
          id="test-input"
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. A customer is asking how to reset their password and is getting frustrated."
        />
      </div>

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={run} disabled={!input.trim() || status === 'running'}>
          {status === 'running' ? 'Working…' : 'Run this through your team'}
        </button>
      </div>

      {events.length > 0 && (
        <ul className="activity-feed" style={{ marginTop: 16 }}>
          {events.map((event, i) => (
            <li key={i} className={`activity-card ${event.type}`}>
              <p className="activity-title">{titleFor(event)}</p>
              {event.data && <p className="activity-body">{event.data}</p>}
            </li>
          ))}
        </ul>
      )}

      <div className="wizard-actions" style={{ marginTop: 16 }}>
        <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/confirm`)}>
          Continue
        </button>
      </div>
    </div>
  )
}
