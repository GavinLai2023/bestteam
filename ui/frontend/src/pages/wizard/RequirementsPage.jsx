import { useEffect, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import BulletEditor from '../../components/BulletEditor'
import ModelPicker from '../../components/ModelPicker'
import { api } from '../../lib/api'

const EMPTY = { summary: '', pain_points: [], goals: [], success_criteria: [], constraints: [], clarifying_questions: [] }

export default function RequirementsPage() {
  const { session, setSession, loading, sessionId } = useOutletContext()
  const navigate = useNavigate()

  const [model, setModel] = useState('')
  const [feedback, setFeedback] = useState('')
  const [draft, setDraft] = useState(EMPTY)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (session?.requirements_json) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync editable draft when AI (re)generates requirements
      setDraft({ ...EMPTY, ...session.requirements_json })
    }
  }, [session?.requirements_json])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  const generate = async () => {
    if (!model || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitRequirements(sessionId, { model, feedback: feedback || undefined })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const confirm = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitRequirements(sessionId, { requirements: draft })
      setSession(updated)
      navigate(`/wizard/${sessionId}/team`)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!session.requirements_json) {
    return (
      <div className="wizard-card">
        <h2>Let's summarize what you need</h2>
        <p className="subtitle">
          Our Business Analyst will read what you wrote and turn it into a short summary you can review and adjust.
        </p>

        {error && <p className="banner banner-error">{error}</p>}

        <ModelPicker value={model} onChange={setModel} label="Which assistant should summarize this?" />

        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={generate} disabled={!model || busy}>
            {busy ? 'Thinking…' : 'Summarize my requirements'}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="wizard-card">
      <h2>Here's what we understood</h2>
      <p className="subtitle">Review and adjust anything that's off, then continue.</p>

      {error && <p className="banner banner-error">{error}</p>}

      {draft.clarifying_questions.length > 0 && (
        <div className="banner banner-info">
          <strong>A couple of quick questions before we design your team:</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
            {draft.clarifying_questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="field">
        <label htmlFor="summary">Summary</label>
        <textarea id="summary" rows={3} value={draft.summary} onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
      </div>

      <div className="field">
        <label>Pain points</label>
        <BulletEditor items={draft.pain_points} onChange={(items) => setDraft({ ...draft, pain_points: items })} placeholder="e.g. replies take too long" />
      </div>

      <div className="field">
        <label>Goals</label>
        <BulletEditor items={draft.goals} onChange={(items) => setDraft({ ...draft, goals: items })} placeholder="e.g. reply within an hour" />
      </div>

      <div className="field">
        <label>What does success look like?</label>
        <BulletEditor items={draft.success_criteria} onChange={(items) => setDraft({ ...draft, success_criteria: items })} placeholder="e.g. fewer escalations" />
      </div>

      <div className="field">
        <label>Constraints</label>
        <BulletEditor items={draft.constraints} onChange={(items) => setDraft({ ...draft, constraints: items })} placeholder="e.g. must stay in English" />
      </div>

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={confirm} disabled={busy}>
          {busy ? 'Saving…' : 'Looks good, continue'}
        </button>
      </div>

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <div className="field">
        <label htmlFor="feedback">
          Not quite right? Tell us what to change <span className="hint">(optional)</span>
        </label>
        <textarea
          id="feedback"
          rows={2}
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder="e.g. We also use Zendesk for tickets, and replies must stay under 150 words."
        />
      </div>
      <ModelPicker value={model} onChange={setModel} label="Which assistant should redo this?" />
      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={generate} disabled={!model || !feedback.trim() || busy}>
          {busy ? 'Thinking…' : 'Regenerate with this feedback'}
        </button>
      </div>
    </div>
  )
}
