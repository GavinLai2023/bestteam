import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import ModelPicker from '../../components/ModelPicker'
import TeamFlow from '../../components/TeamFlow'
import { api } from '../../lib/api'

export default function TeamPage() {
  const { session, setSession, loading, sessionId } = useOutletContext()
  const navigate = useNavigate()

  const [model, setModel] = useState('')
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  const generate = async (withFeedback) => {
    if (!model || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitSpecification(sessionId, {
        model,
        feedback: withFeedback ? feedback || undefined : undefined,
      })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Let's design your team</h2>
        <p className="subtitle">
          Our Solution Architect will turn your requirements into a small team of AI "employees" suited to the job.
        </p>

        {error && <p className="banner banner-error">{error}</p>}

        <ModelPicker value={model} onChange={setModel} label="Which assistant should design this?" />

        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => generate(false)} disabled={!model || busy}>
            {busy ? 'Designing…' : 'Design my team'}
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json

  return (
    <div className="wizard-card">
      <h2>Meet your team</h2>
      <p className="subtitle">Here's the team we've put together for "{spec.name}", and how they'll work together.</p>

      {error && <p className="banner banner-error">{error}</p>}

      <TeamFlow specification={spec} />

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/refine`)}>
          Looks good, continue
        </button>
      </div>

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <div className="field">
        <label htmlFor="team-feedback">
          Want a different team? Tell us what to change <span className="hint">(optional)</span>
        </label>
        <textarea
          id="team-feedback"
          rows={2}
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder="e.g. Add someone to translate replies into Spanish."
        />
      </div>
      <ModelPicker value={model} onChange={setModel} label="Which assistant should redo this?" />
      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={() => generate(true)} disabled={!model || !feedback.trim() || busy}>
          {busy ? 'Redesigning…' : 'Redesign with this feedback'}
        </button>
      </div>
    </div>
  )
}
