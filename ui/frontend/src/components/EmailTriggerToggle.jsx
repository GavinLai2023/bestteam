import { useEffect, useState } from 'react'
import { api } from '../lib/api'

// Org-level opt-in: run this deployed email team automatically on new mail.
// Off by default; shown on the Deploy page once the team is live.
export default function EmailTriggerToggle({ workflowName }) {
  const [trigger, setTrigger] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch((e) => setError(e.message))
  }, [])

  if (!trigger) return error ? <p className="banner banner-error">{error}</p> : null

  const onForThis = trigger.enabled && trigger.workflow_name === workflowName

  const toggle = async () => {
    setBusy(true)
    setError(null)
    try {
      setTrigger(await api.setEmailTrigger({ workflow_name: workflowName, enabled: !onForThis }))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wizard-card" style={{ background: '#f9fafb' }}>
      <h3>Automatic runs</h3>
      <p className="subtitle">
        Let "{workflowName}" watch the inbox on its own: it checks for new email every few minutes
        and drafts replies without you having to start it — up to {trigger.daily_cap} automatic runs
        per day. It still only ever saves drafts; it never sends.
      </p>

      {error && <p className="banner banner-error">{error}</p>}
      {onForThis && trigger.status === 'paused_cap' && (
        <p className="banner banner-error">
          Paused — today's limit of {trigger.daily_cap} automatic runs was reached. Runs resume tomorrow.
        </p>
      )}
      {onForThis && trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={toggle} disabled={busy}>
          {busy ? 'Saving…' : onForThis ? 'Turn off automatic runs' : 'Run automatically when new email arrives'}
        </button>
        {onForThis && trigger.status === 'active' && (
          <span className="hint">On — watching for new email.</span>
        )}
      </div>
    </div>
  )
}
