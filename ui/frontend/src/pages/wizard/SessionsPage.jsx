import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/dateFormat'
import '../../components/WizardLayout.css'
import './SessionsPage.css'

// Sessions that haven't reached the Specification stage yet have no team
// name and nowhere sensible to resume into.
const RESUMABLE_STATUSES = new Set(['spec', 'solution', 'testing', 'deployed'])

// Spec/solution/testing all resume into the same Confirm page with identical
// content -- there's no customer-visible difference between them (testing in
// particular is set just from trying the team once on the Preview page, not
// from any deliberate stage change), so they're one friendly bucket rather
// than three technical-sounding statuses.
const STATUS_ORDER = ['deployed', 'in_progress']

const STATUS_LABELS = {
  deployed: 'Live',
  in_progress: 'In Progress',
}

const STATUS_EXPLANATIONS = {
  deployed: 'Live -- this team is deployed and ready for your organization to use.',
  in_progress: "Still being built -- you're designing, reviewing, or trying out this team before making it live.",
}

function bucketFor(status) {
  return status === 'deployed' ? 'deployed' : 'in_progress'
}

// Short forms of EmailTriggerActivity's STATUS_LABELS, for a one-line card
// tag rather than a full status block (see the Activity page for the full view).
const AUTOMATION_STATUS_LABELS = {
  active: 'Automation on — watching for new email',
  paused_cap: 'Automation paused — daily limit reached',
  error: 'Automation problem — checking mailbox',
  disabled: 'Automation paused',
}

function resumePathFor(session) {
  // A deployed workflow with no builder session (deployed straight through
  // the admin Advanced/CRUD page, see builder.py's
  // _synthetic_session_for_workflow) has no wizard flow to resume into --
  // send it to Run a Team, pre-selected, instead.
  if (session.id == null) {
    return `/?workflow=${encodeURIComponent(session.specification_json?.name ?? '')}`
  }
  return session.status === 'deployed' ? `/wizard/${session.id}/deploy` : `/wizard/${session.id}/confirm`
}

// A team's own one-sentence friendly_description (written for a
// non-technical reader) if the architect produced one, else the customer's
// original prompt -- that's an intent statement, not a description of what
// the team does, but it's better than nothing while a session is mid-flow.
function descriptionFor(session) {
  return session.specification_json?.teams?.[0]?.friendly_description || session.intent_text
}

export default function SessionsPage() {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // The org has at most one automatic trigger (see EmailTriggerToggle); a
  // missing/failed fetch just means no card gets the tag (best-effort).
  const [trigger, setTrigger] = useState(null)
  const [openStatus, setOpenStatus] = useState(null)

  useEffect(() => {
    api
      .listSessions()
      .then((data) => setSessions(data.sessions.filter((s) => RESUMABLE_STATUSES.has(s.status))))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
    api.getEmailTrigger().then(setTrigger).catch(() => {})
  }, [])

  const statusGroups = STATUS_ORDER.map((bucket) => ({
    status: bucket,
    sessions: sessions.filter((s) => bucketFor(s.status) === bucket),
  })).filter((group) => group.sessions.length > 0)

  const handleDelete = async (session) => {
    const label = session.specification_json?.name ?? session.intent_text
    if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return
    try {
      await api.deleteSession(session.id)
      setSessions((prev) => prev.filter((s) => s.id !== session.id))
    } catch (e) {
      setError(e.message)
    }
  }

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
        statusGroups.map((group) => (
          <section key={group.status} className="session-status-group">
            <div className="session-status-header">
              <h2>
                {STATUS_LABELS[group.status]} ({group.sessions.length})
              </h2>
              <button
                type="button"
                className="status-help-button"
                aria-label={`What does ${STATUS_LABELS[group.status]} mean?`}
                onClick={() => setOpenStatus((s) => (s === group.status ? null : group.status))}
              >
                ?
              </button>
            </div>
            {openStatus === group.status && (
              <p className="hint status-help-text">{STATUS_EXPLANATIONS[group.status]}</p>
            )}
            <ul className="session-list">
              {group.sessions.map((session) => {
                const teamName = session.specification_json?.name
                const isAutomated = trigger?.enabled && teamName && trigger.workflow_name === teamName
                return (
                  <li key={session.id ?? `workflow:${teamName}`} className="session-item">
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{teamName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {AUTOMATION_STATUS_LABELS[trigger.status] ?? trigger.status}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">Updated {formatDateTime(session.updated_at)}</span>
                      </div>
                    </button>
                    {session.workflow_id == null && (
                      <button
                        type="button"
                        className="session-delete-button"
                        aria-label="Delete"
                        title="Delete"
                        onClick={() => handleDelete(session)}
                      >
                        <svg
                          viewBox="0 0 24 24"
                          width="16"
                          height="16"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <polyline points="3 6 5 6 21 6" />
                          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                          <path d="M10 11v6" />
                          <path d="M14 11v6" />
                          <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                        </svg>
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>
        ))
      )}
    </div>
  )
}
