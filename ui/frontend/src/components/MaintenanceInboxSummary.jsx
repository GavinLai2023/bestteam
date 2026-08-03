import { useEffect, useState } from 'react'
import { api } from '../lib/api'

// Today's Property Maintenance Inbox counters for the Activity page's
// Automations tab (spec section 6.2). Renders nothing until loaded, nothing
// at all for an org that has never used this template (summary.ever_used),
// and a light "nothing processed yet today" line rather than an empty card
// for an org that uses it but has no results today -- most orgs on this
// deployment don't use this vertical at all, so a blank/zero card would be
// confusing rather than reassuring.
// The backend defaults to a UTC calendar day when no `date` is given, which
// shows the wrong day's counts around local midnight for an org far from
// UTC (Codex review finding). There's no org-level timezone setting in this
// app, so the fix is the browser's own local date rather than a new backend
// feature -- the endpoint already accepts `date` for exactly this override.
function _todayLocal() {
  const now = new Date()
  const yyyy = now.getFullYear()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

export default function MaintenanceInboxSummary() {
  const [summary, setSummary] = useState(undefined) // undefined = still loading
  const [error, setError] = useState(false)

  useEffect(() => {
    api
      .automationResultsSummary(_todayLocal())
      .then(setSummary)
      .catch(() => setError(true))
  }, [])

  if (error) return null // auxiliary card -- fail silently rather than block the page
  if (summary === undefined) return null
  if (!summary.ever_used) return null

  if (summary.emails_read === 0) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <h3>Property Maintenance Inbox — today</h3>
        <p className="hint">No maintenance emails processed yet today.</p>
      </div>
    )
  }

  return (
    <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
      <h3>Property Maintenance Inbox — today</h3>
      <dl className="maintenance-summary-grid">
        <div>
          <dt>Emails read</dt>
          <dd>{summary.emails_read}</dd>
        </div>
        <div>
          <dt>Maintenance-related</dt>
          <dd>{summary.maintenance_related}</dd>
        </div>
        <div>
          <dt>Drafts created</dt>
          <dd>{summary.drafts_created}</dd>
        </div>
        <div>
          <dt>Needs attention</dt>
          <dd>{summary.needs_attention}</dd>
        </div>
        <div>
          <dt>Possible emergency</dt>
          <dd>{summary.possible_emergency}</dd>
        </div>
        <div>
          <dt>Skipped (non-maintenance)</dt>
          <dd>{summary.skipped_non_maintenance}</dd>
        </div>
        <div>
          <dt>Errors</dt>
          <dd>{summary.errors}</dd>
        </div>
      </dl>
    </div>
  )
}
