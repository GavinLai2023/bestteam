import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailBudgetSettings from '../../components/EmailBudgetSettings'
import EmailConnect from '../../components/EmailConnect'
import EmailFilterSettings from '../../components/EmailFilterSettings'
import EmailTriggerToggle from '../../components/EmailTriggerToggle'
import { api } from '../../lib/api'
import type { WizardOutletContext } from '../../lib/types'

export default function DeployPage() {
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [emailConnected, setEmailConnected] = useState(false)

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Go live</h2>
        <p className="subtitle">Design your team first, then come back here to launch it.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json

  const deploy = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.deploySession(sessionId!)
      setSession(updated)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (session.status === 'deployed') {
    return (
      <div className="wizard-card">
        <h2>Your team is live 🎉</h2>
        <p className="banner banner-success">"{spec.name}" is up and running and ready to take on real requests.</p>
        {session.uses_email && <EmailConnect />}
        {session.uses_email && <EmailTriggerToggle pipelineName={spec.name} />}
        {/* This team's mail-handling settings live here, not on the Activity
            page's Automations tab -- they configure this team, the same way
            EmailConnect/EmailTriggerToggle above already do. */}
        {session.uses_email && <EmailFilterSettings />}
        {session.uses_email && <EmailBudgetSettings />}
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/confirm`)}>
            Make more adjustments
          </button>
          <button className="btn btn-secondary" onClick={() => navigate(`/run?pipeline=${encodeURIComponent(spec.name)}`)}>
            Run a team
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="wizard-card">
      <h2>Ready to go live?</h2>
      <p className="subtitle">
        Once you launch, "{spec.name}" will be available to handle real requests. You can always come back and adjust
        it later.
      </p>

      {session.uses_email && (
        <EmailConnect onChange={() => setError(null)} onStatusChange={setEmailConnected} />
      )}

      {error && <p className="banner banner-error">{error}</p>}

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={deploy} disabled={busy || (session.uses_email && !emailConnected)}>
          {busy ? 'Launching…' : 'Launch my team'}
        </button>
      </div>
      {session.uses_email && !emailConnected && (
        <p className="hint">Connect your mailbox above before you can launch this team.</p>
      )}
    </div>
  )
}
