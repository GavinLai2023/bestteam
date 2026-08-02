import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'

const STATUS_META = {
  active: { badge: 'Active', text: 'Watching for new email.' },
  off: { badge: 'Off', text: 'Automatic runs are turned off.' },
  disabled: { badge: 'Paused', text: 'Paused by the operator.' },
  paused_cap: { badge: 'Paused', text: "Today's run limit was reached -- resumes tomorrow." },
  error: { badge: 'Problem', text: 'Problem checking the mailbox.' },
}

// How often to re-poll while mounted on the Activity page's Automations tab.
// The backend's own poller checks mail every BESTTEAM_TRIGGER_POLL_SECONDS
// (default 120s) -- refreshing faster than that would just show stale data
// sooner, so this trades a little staleness for not hammering the endpoint.
const REFRESH_INTERVAL_MS = 30_000

// Org-level automatic-runs status, shown on the Activity page's Automations
// tab. Always shows a status card once loaded -- including "off" -- so a
// deliberately-disabled trigger reads differently from never having
// configured one. Recent autonomous runs live on the Runs tab (filter:
// "Automatic only") instead of being duplicated here; `onViewRuns` jumps
// there pre-filtered.
export default function EmailTriggerActivity({ onViewRuns }) {
  const [trigger, setTrigger] = useState(undefined) // undefined = still loading
  const [statusFailed, setStatusFailed] = useState(false)

  useEffect(() => {
    const load = () => {
      api.getEmailTrigger().then(setTrigger).catch(() => setStatusFailed(true))
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

  if (!trigger.workflow_name) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <h3>Automatic runs</h3>
        <p className="hint">
          No automatic runs configured yet. Connect a mailbox and turn one on from a team's
          Deploy page in the Team Builder.
        </p>
      </div>
    )
  }

  const meta = STATUS_META[trigger.status] ?? { badge: trigger.status, text: '' }

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <div className="trigger-status-header">
        <h3>Automatic runs — "{trigger.workflow_name}"</h3>
        <span className="status-badge">{meta.badge}</span>
      </div>
      <p className="subtitle">{meta.text}</p>
      <p className="hint">Triggers when new email arrives in the connected mailbox.</p>
      {trigger.last_checked_at && (
        <p className="hint">
          Last checked: {formatDateTime(trigger.last_checked_at)}
        </p>
      )}
      {trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}
      <p className="hint">
        Turn this on, off, or point it at a different team from that team's Deploy page in the
        Team Builder.
      </p>
      {onViewRuns && (
        <button type="button" className="btn-link" onClick={onViewRuns}>
          View automatic runs
        </button>
      )}
    </div>
  )
}
