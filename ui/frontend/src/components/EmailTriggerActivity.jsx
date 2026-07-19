import { useEffect, useState } from 'react'
import { api } from '../lib/api'

const STATUS_LABELS = {
  active: 'Active — watching for new email',
  paused_cap: 'Paused — daily limit reached (resumes tomorrow)',
  error: 'Problem checking the mailbox',
  disabled: 'Paused by the operator',
}

// Org-level automatic-runs status + recent autonomous activity, shown on
// "My teams". Renders nothing while automatic runs are off.
export default function EmailTriggerActivity() {
  const [trigger, setTrigger] = useState(null)
  const [runs, setRuns] = useState([])

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch(() => setTrigger(null))
    api
      .emailTriggerActivity()
      .then((d) => setRuns(d.runs.filter((r) => r.autonomous).slice(0, 10)))
      .catch(() => setRuns([]))
  }, [])

  if (!trigger?.enabled) return null

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <h3>Automatic runs — "{trigger.workflow_name}"</h3>
      <p className="subtitle">{STATUS_LABELS[trigger.status] ?? trigger.status}</p>
      {trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}
      {runs.length === 0 ? (
        <p className="hint">No automatic runs yet — they'll show up here when new email arrives.</p>
      ) : (
        <ul className="session-list">
          {runs.map((r) => (
            <li key={r.id} className="hint">
              <span className="status-badge">{r.status}</span>{' '}
              {r.started_at ? new Date(r.started_at).toLocaleString() : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
