import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import ModelPicker from '../../components/ModelPicker'
import TeamFlow from '../../components/TeamFlow'
import { api } from '../../lib/api'

export default function RefinePage() {
  const { session, setSession, loading, sessionId } = useOutletContext()
  const navigate = useNavigate()

  const [model, setModel] = useState('')
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Refine your team</h2>
        <p className="subtitle">Design your team first, then come back here to fine-tune it.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/team`)}>
            Go to "Meet your team"
          </button>
        </div>
      </div>
    )
  }

  const applyFeedback = async () => {
    if (!model || !feedback.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitSolution(sessionId, { feedback: feedback.trim(), model })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const spec = session.specification_json
  const history = (session.feedback_history ?? []).filter((entry) => entry.stage === 'solution')

  return (
    <div className="wizard-card">
      <h2>Anything to fine-tune?</h2>
      <p className="subtitle">
        Take another look at your team. If something's off, describe the change in plain language and we'll adjust the
        design.
      </p>

      {error && <p className="banner banner-error">{error}</p>}

      <TeamFlow specification={spec} />

      {history.length > 0 && (
        <div className="banner banner-info" style={{ marginTop: 16 }}>
          <strong>Adjustments so far:</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
            {history.map((entry, i) => (
              <li key={i}>{entry.note}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="field" style={{ marginTop: 16 }}>
        <label htmlFor="solution-feedback">
          What should change? <span className="hint">(optional)</span>
        </label>
        <textarea
          id="solution-feedback"
          rows={3}
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder="e.g. Have the team check our FAQ document before replying."
        />
      </div>
      <ModelPicker value={model} onChange={setModel} label="Which assistant should make this change?" />

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={applyFeedback} disabled={!model || !feedback.trim() || busy}>
          {busy ? 'Updating…' : 'Apply this change'}
        </button>
        <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/test`)}>
          Continue to testing
        </button>
      </div>
    </div>
  )
}
