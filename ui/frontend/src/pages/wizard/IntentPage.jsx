import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'

export default function IntentPage() {
  const navigate = useNavigate()
  const [intentText, setIntentText] = useState('')
  const [asIsText, setAsIsText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const start = async () => {
    if (!intentText.trim() || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const session = await api.createSession(intentText.trim(), asIsText.trim())
      navigate(`/wizard/${session.id}/requirements`)
    } catch (e) {
      setError(e.message)
      setSubmitting(false)
    }
  }

  return (
    <div className="wizard-card">
      <h2>Tell us about your challenge</h2>
      <p className="subtitle">
        Describe what you're hoping an AI team could take off your plate. No technical detail needed — plain
        language is perfect.
      </p>

      {error && <p className="banner banner-error">{error}</p>}

      <div className="field">
        <label htmlFor="intent">What do you want help with?</label>
        <textarea
          id="intent"
          rows={5}
          value={intentText}
          onChange={(e) => setIntentText(e.target.value)}
          placeholder="e.g. We get dozens of customer support emails a day and can't keep up with replies."
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
        />
      </div>

      <div className="wizard-actions">
        <button className="btn btn-primary" onClick={start} disabled={!intentText.trim() || submitting}>
          {submitting ? 'Starting…' : 'Start building my team'}
        </button>
      </div>
    </div>
  )
}
