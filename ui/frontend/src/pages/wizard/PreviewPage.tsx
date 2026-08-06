import { useEffect, useRef, useState } from 'react'
import type { ComponentType } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailConnectRaw from '../../components/EmailConnect'
import TeamFlow from '../../components/TeamFlow'
import { WS_BASE, api } from '../../lib/api'
import type { TraceEvent, WizardOutletContext } from '../../lib/types'

// EmailConnect isn't converted to TypeScript until Task 8. Until then, tsc
// infers its untyped destructured `{ onChange, onStatusChange }` parameter
// as a *required* object type (both props are optional at runtime -- the
// component calls them with `?.()`), so rendering it with no props here
// (as the current JSX does) would otherwise fail to compile. This narrows
// the inferred type back to optional, matching actual runtime behavior; no
// change to EmailConnect.jsx itself.
const EmailConnect = EmailConnectRaw as ComponentType<{
  onChange?: () => void
  onStatusChange?: (connected: boolean) => void
}>

type Status = 'idle' | 'running' | 'completed' | 'failed'

export default function PreviewPage() {
  const { session, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [input, setInput] = useState('')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

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

  const friendlyName = (agentName: string) => {
    const agent = agentsByName[agentName]
    return agent?.display_name || agentName
  }

  const titleFor = (event: TraceEvent) => {
    switch (event.type) {
      case 'run_started':
        return 'Your team got started'
      case 'agent_completed':
        return `${friendlyName(event.agent ?? '')} finished their part`
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
      const { run_id: runId } = await api.createTestRun(sessionId!, input.trim())

      const { ticket } = await api.createWsTicket()
      const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
      wsRef.current = ws
      ws.onmessage = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as TraceEvent
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
      setError((e as Error).message)
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

      {session.uses_email && (
        <>
          <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />
          <EmailConnect />
          <p className="hint" style={{ marginTop: 8 }}>
            Connect your mailbox to try the team against your real inbox below — or skip for now and
            connect before you go live.
          </p>
        </>
      )}

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
              {event.data && (
                <p className="activity-body">
                  {typeof event.data === 'string' ? event.data : JSON.stringify(event.data)}
                </p>
              )}
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
