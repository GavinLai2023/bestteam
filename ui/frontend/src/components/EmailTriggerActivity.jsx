import { useEffect, useState } from 'react'
import { api } from '../lib/api'

const STATUS_LABELS = {
  active: 'Active — watching for new email',
  paused_cap: 'Paused — daily limit reached (resumes tomorrow)',
  error: 'Problem checking the mailbox',
  disabled: 'Paused by the operator',
}

// How often to re-poll while mounted on the Activity page's Automations tab.
// The backend's own poller checks mail every BESTTEAM_TRIGGER_POLL_SECONDS
// (default 120s) -- refreshing faster than that would just show stale data
// sooner, so this trades a little staleness for not hammering the endpoint.
const REFRESH_INTERVAL_MS = 30_000

// Org-level automatic-runs status + recent autonomous activity, shown on the
// Activity page's Automations tab. Renders nothing while loading or while
// automatic runs are off (most teams never turn this on); a fetch failure
// gets its own banner so it is never mistaken for either of those.
export default function EmailTriggerActivity() {
  const [trigger, setTrigger] = useState(undefined) // undefined = still loading
  const [statusFailed, setStatusFailed] = useState(false)
  const [runs, setRuns] = useState([])
  const [activityFailed, setActivityFailed] = useState(false)

  useEffect(() => {
    const load = () => {
      api.getEmailTrigger().then(setTrigger).catch(() => setStatusFailed(true))
      api
        .emailTriggerActivity()
        .then((d) => setRuns(d.runs.filter((r) => r.autonomous).slice(0, 10)))
        .catch(() => setActivityFailed(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  if (statusFailed) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <p className="banner banner-error">
          Couldn't load automatic-run status. Refresh the page to try again.
        </p>
      </div>
    )
  }

  if (trigger === undefined) return null // still loading -- avoid a flash
  if (!trigger.enabled) return null // genuinely off -- nothing to show

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <h3>Automatic runs — "{trigger.workflow_name}"</h3>
      <p className="subtitle">{STATUS_LABELS[trigger.status] ?? trigger.status}</p>
      {trigger.last_checked_at && (
        <p className="hint">
          Last checked: {new Date(trigger.last_checked_at).toLocaleString()}
        </p>
      )}
      {trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}
      {activityFailed ? (
        <p className="hint">Couldn't load recent activity. Refresh the page to try again.</p>
      ) : runs.length === 0 ? (
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
