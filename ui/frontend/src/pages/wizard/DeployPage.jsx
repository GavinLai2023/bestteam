import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'

export default function DeployPage() {
  const { session, setSession, loading, sessionId } = useOutletContext()
  const navigate = useNavigate()

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

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
      const updated = await api.deploySession(sessionId)
      setSession(updated)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (session.status === 'deployed') {
    return (
      <div className="wizard-card">
        <h2>Your team is live 🎉</h2>
        <p className="banner banner-success">"{spec.name}" is up and running and ready to take on real requests.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate(`/?workflow=${encodeURIComponent(spec.name)}`)}>
            Talk to your team
          </button>
          <button className="btn btn-secondary" onClick={() => navigate(`/wizard/${sessionId}/confirm`)}>
            Make more adjustments
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

      {error && <p className="banner banner-error">{error}</p>}

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={deploy} disabled={busy}>
          {busy ? 'Launching…' : 'Launch my team'}
        </button>
      </div>
    </div>
  )
}
