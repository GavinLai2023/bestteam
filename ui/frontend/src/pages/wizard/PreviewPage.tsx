import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailConnect from '../../components/EmailConnect'
import TeamFlow from '../../components/TeamFlow'
import { WS_BASE, api } from '../../lib/api'
import type { TraceEvent, WizardOutletContext } from '../../lib/types'

type Status = 'idle' | 'running' | 'completed' | 'failed'

export default function PreviewPage() {
  const { t } = useTranslation()
  const { session, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [input, setInput] = useState('')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => () => wsRef.current?.close(), [])

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.preview.title')}</h2>
        <p className="subtitle">{t('wizard.needMore')}</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            {t('common.startOver')}
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
        return t('traceEvents.started')
      case 'agent_completed':
        return t('traceEvents.agentDone', { agent: friendlyName(event.agent ?? '') })
      case 'run_completed':
        return t('traceEvents.completed')
      case 'run_failed':
        return t('traceEvents.failed')
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
        setError(t('wizard.preview.lostConnection'))
      }
      ws.onclose = () => {
        setStatus((current) => {
          if (current === 'running') {
            setError(t('wizard.preview.lostConnection'))
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
      <h2>{t('wizard.preview.title')}</h2>
      <p className="subtitle">{t('wizard.preview.subtitle', { name: spec.name })}</p>

      {error && <p className="banner banner-error">{error}</p>}

      <TeamFlow specification={spec} />

      {session.uses_email && (
        <>
          <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />
          <EmailConnect />
          <p className="hint" style={{ marginTop: 8 }}>{t('wizard.preview.mailboxHint')}</p>
        </>
      )}

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <h3>{t('wizard.preview.tryThemOut')}</h3>
      <div className="field">
        <label htmlFor="test-input">{t('wizard.preview.taskLabel')}</label>
        <textarea
          id="test-input"
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t('wizard.preview.taskPlaceholder')}
        />
      </div>

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={run} disabled={!input.trim() || status === 'running'}>
          {status === 'running' ? t('common.working') : t('wizard.preview.run')}
        </button>
      </div>

      {events.length > 0 && (
        <ul className="activity-feed" style={{ marginTop: 16 }}>
          {events.map((event, i) => (
            <li key={i} className={`activity-card ${event.type}`}>
              <p className="activity-title">{titleFor(event)}</p>
              {/* Stringifies rather than crashes if `event.data` is ever a non-string
                  object here (unverified in practice) -- unlike MonitorPage/RunDetail's
                  `data as string` cast, which is safe because it only renders `data` for
                  terminal events, where the backend's event-emission contract guarantees
                  a string. */}
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
        <button
          className="btn btn-primary"
          onClick={() => navigate(`/wizard/${sessionId}/confirm`)}
          disabled={status === 'running'}
        >
          {t('common.continue')}
        </button>
      </div>
    </div>
  )
}
