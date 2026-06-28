import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import '../../components/WizardLayout.css'
import './SessionsPage.css'

// Sessions that haven't reached the Specification stage yet have no team
// name and nowhere sensible to resume into.
const RESUMABLE_STATUSES = new Set(['spec', 'solution', 'testing', 'deployed'])

function resumePathFor(session) {
  return session.status === 'deployed' ? `/wizard/${session.id}/deploy` : `/wizard/${session.id}/confirm`
}

export default function SessionsPage() {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .listSessions()
      .then((data) => setSessions(data.sessions.filter((s) => RESUMABLE_STATUSES.has(s.status))))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
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
          {sessions.map((session) => (
            <li key={session.id}>
              <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                <h2>{session.specification_json?.name ?? session.intent_text}</h2>
                <p className="subtitle">{session.intent_text}</p>
                <div className="session-card-footer">
                  <span className="status-badge">{session.status}</span>
                  <span className="session-updated">Updated {new Date(session.updated_at).toLocaleString()}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
