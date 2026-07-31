import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import '../../components/WizardLayout.css'
import './SessionsPage.css'

// Sessions that haven't reached the Specification stage yet have no team
// name and nowhere sensible to resume into.
const RESUMABLE_STATUSES = new Set(['spec', 'solution', 'testing', 'deployed'])

// Short forms of EmailTriggerActivity's STATUS_LABELS, for a one-line card
// tag rather than a full status block (see the Activity page for the full view).
const AUTOMATION_STATUS_LABELS = {
  active: 'Automation on — watching for new email',
  paused_cap: 'Automation paused — daily limit reached',
  error: 'Automation problem — checking mailbox',
  disabled: 'Automation paused',
}

function resumePathFor(session) {
  return session.status === 'deployed' ? `/wizard/${session.id}/deploy` : `/wizard/${session.id}/confirm`
}

export default function SessionsPage() {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // The org has at most one automatic trigger (see EmailTriggerToggle); a
  // missing/failed fetch just means no card gets the tag (best-effort).
  const [trigger, setTrigger] = useState(null)

  useEffect(() => {
    api
      .listSessions()
      .then((data) => setSessions(data.sessions.filter((s) => RESUMABLE_STATUSES.has(s.status))))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
    api.getEmailTrigger().then(setTrigger).catch(() => {})
  }, [])

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>My teams</h1>
        <p>Pick up where you left off, or make adjustments to a team you've already built.</p>
      </header>

      {error && <p className="banner banner-error">{error}</p>}

      {loading ? (
        <p className="hint">Loading…</p>
      ) : sessions.length === 0 ? (
        <p className="hint">No teams yet — build one to see it here.</p>
      ) : (
        <ul className="session-list">
          {sessions.map((session) => {
            const teamName = session.specification_json?.name
            const isAutomated = trigger?.enabled && teamName && trigger.workflow_name === teamName
            return (
              <li key={session.id}>
                <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                  <h2>{teamName ?? session.intent_text}</h2>
                  <p className="subtitle">{session.intent_text}</p>
                  {isAutomated && (
                    <p className="hint automation-tag">
                      {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                    </p>
                  )}
                  <div className="session-card-footer">
                    <span className="status-badge">{session.status}</span>
                    <span className="session-updated">Updated {new Date(session.updated_at).toLocaleString()}</span>
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
