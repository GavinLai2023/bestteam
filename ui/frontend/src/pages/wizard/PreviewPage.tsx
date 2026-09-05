import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailConnect from '../../components/EmailConnect'
import RunProgressStrip from '../../components/RunProgressStrip'
import TeamFlow from '../../components/TeamFlow'
import { WS_BASE, api } from '../../lib/api'
import { TERMINAL_TYPES, useDetailedEventLine } from '../../lib/traceEvents'
import type { TraceEvent, WizardOutletContext } from '../../lib/types'
import { useWorkingAgents } from '../../lib/workingAgents'

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

  // Resolves an agent's technical name to the one the wizard gave it. Reads
  // the session directly rather than the `spec` local below, because a hook
  // cannot sit after this component's early returns.
  const friendlyName = (agentName: string) =>
    (session?.specification_json?.agents ?? []).find((a) => a.name === agentName)?.display_name ||
    agentName
  // Shared with the Activity page's expanded run view, so the wizard's test
  // run and every run after it are narrated the same way. It also returns null
  // for the platform's own machinery, which this feed used to render as a raw
  // type string with `JSON.stringify(event.data)` underneath.
  const detailedLine = useDetailedEventLine(friendlyName)
  const { working, completedAgents } = useWorkingAgents(events)

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

      {status === 'running' && (
        <RunProgressStrip
          working={working}
          completedAgents={completedAgents}
          agentCount={spec.agents.length || undefined}
          displayNameFor={friendlyName}
        />
      )}

      {events.length > 0 && (
        <ul className="activity-feed" style={{ marginTop: 16 }}>
          {events.map((event, i) => {
            const line = detailedLine(event)
            if (!line) return null
            // A terminal event's `data` IS the run's answer, which is the whole
            // point of a test run; every other event's body comes from the
            // narrator, never from the raw payload.
            const body =
              TERMINAL_TYPES.includes(event.type) && typeof event.data === 'string'
                ? event.data
                : line.detail
            return (
              <li key={i} className={`activity-card ${event.type}`}>
                <p className="activity-title">{line.title}</p>
                {body && <p className="activity-body">{body}</p>}
              </li>
            )
          })}
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
