import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'

const STAGE_LABELS = {
  creating: 'Setting things up…',
  requirements: 'Getting to know your business…',
  specification: 'Putting your team together…',
}

export default function IntentPage() {
  const navigate = useNavigate()
  const { entries } = useModelCatalog()
  const [intentText, setIntentText] = useState('')
  const [asIsText, setAsIsText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [stage, setStage] = useState(null) // null | 'creating' | 'requirements' | 'specification'
  const [error, setError] = useState(null)
  const [sessionId, setSessionId] = useState(null)

  const buildSpecification = async (id) => {
    setStage('specification')
    try {
      await api.submitSpecification(id, { model: pickDefaultModel(entries) })
      navigate(`/wizard/${id}/preview`)
    } catch (e) {
      setError(e.message)
      setSubmitting(false)
      setStage(null)
    }
  }

  const start = async () => {
    if (!intentText.trim() || submitting) return
    setSubmitting(true)
    setError(null)
    const model = pickDefaultModel(entries)

    let id = sessionId
    if (!id) {
      setStage('creating')
      try {
        const session = await api.createSession(intentText.trim(), asIsText.trim())
        id = session.id
        setSessionId(id)
      } catch (e) {
        setError(e.message)
        setSubmitting(false)
        setStage(null)
        return
      }
    }

    // Best-effort: the Requirements summary is a nice-to-have internal
    // artifact. /specification degrades gracefully (falls back to the raw
    // intent/as-is text) if this fails, so don't block on it.
    setStage('requirements')
    try {
      await api.submitRequirements(id, { model })
    } catch {
      // ignored — non-blocking
    }

    await buildSpecification(id)
  }

  const retry = () => {
    setError(null)
    setSubmitting(true)
    if (sessionId) {
      buildSpecification(sessionId)
    } else {
      start()
    }
  }

  return (
    <div className="wizard-card">
      <h2>Tell us about your challenge</h2>
      <p className="subtitle">
        Describe what you're hoping an AI team could take off your plate. No technical detail needed — plain
        language is perfect.
      </p>

      {error && (
        <div className="banner banner-error">
          {error}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retry} disabled={submitting}>
              Try again
            </button>
          </div>
        </div>
      )}

      <div className="field">
        <label htmlFor="intent">What do you want help with?</label>
        <textarea
          id="intent"
          rows={5}
          value={intentText}
          onChange={(e) => setIntentText(e.target.value)}
          placeholder="e.g. We get dozens of customer support emails a day and can't keep up with replies."
          disabled={submitting}
        />
      </div>

      <div className="field">
        <label htmlFor="as-is">
          How do you handle this today? <span className="hint">(optional)</span>
        </label>
        <textarea
          id="as-is"
          rows={4}
          value={asIsText}
          onChange={(e) => setAsIsText(e.target.value)}
          placeholder="e.g. One person reads every email and replies manually using a few canned templates."
          disabled={submitting}
        />
      </div>

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={start} disabled={!intentText.trim() || submitting}>
          {submitting ? STAGE_LABELS[stage] ?? 'Starting…' : 'Start building my team'}
        </button>
      </div>
    </div>
  )
}
