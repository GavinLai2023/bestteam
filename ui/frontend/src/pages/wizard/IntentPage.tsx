import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'

const STAGE_LABELS: Record<string, string> = {
  creating: 'Setting things up…',
  requirements: 'Getting to know your business…',
}

const UPLOAD_LABELS: Record<string, string> = {
  transcribing: 'Transcribing interview…',
  extracting: 'Extracting key points…',
}

const ACCEPTED_AUDIO = '.mp3,.mp4,.m4a,.wav,.webm,.mpeg,.mpga'

type Stage = null | 'creating' | 'requirements'
type UploadStage = null | 'transcribing' | 'extracting' | 'done'

export default function IntentPage() {
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  // A fetch failure and a successfully-loaded-but-empty catalog (no real
  // model configured yet) both mean "there's no real model to generate
  // with" -- neither should silently fall through to pickDefaultModel's
  // `fake:ok` last resort in the customer-facing wizard.
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)
  const [intentText, setIntentText] = useState('')
  const [asIsText, setAsIsText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [stage, setStage] = useState<Stage>(null)
  const [error, setError] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [uploadStage, setUploadStage] = useState<UploadStage>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [transcript, setTranscript] = useState<string | null>(null)

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || catalogLoading || catalogUnavailable) return
    e.target.value = '' // reset so the same file can be re-selected

    setUploadError(null)
    setUploadStage('transcribing')

    // After 5 s, advance the label to hint the extraction phase is underway.
    const timer = setTimeout(
      () => setUploadStage((s) => (s === 'transcribing' ? 'extracting' : s)),
      5000,
    )
    try {
      const result = await api.transcribeInterview(file, pickDefaultModel(entries))
      setIntentText(result.intent_text)
      setAsIsText(result.as_is_text)
      setTranscript(result.transcript)
      setSessionId(null) // force a fresh session with the new intent text
      setUploadStage('done')
    } catch (err) {
      setUploadError((err as Error).message)
      setUploadStage(null)
    } finally {
      clearTimeout(timer)
    }
  }

  const start = async () => {
    if (!intentText.trim() || submitting || catalogLoading || catalogUnavailable) return
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
        setError((e as Error).message)
        setSubmitting(false)
        setStage(null)
        return
      }
    }

    if (!id) return

    // Best-effort: the Requirements summary is a nice-to-have internal
    // artifact. /specification degrades gracefully (falls back to the raw
    // intent/as-is text) if this fails, so don't block on it.
    setStage('requirements')
    try {
      await api.submitRequirements(id, { model })
    } catch {
      // ignored — non-blocking
    }

    navigate(`/wizard/${id}/documents`)
  }

  const retry = () => {
    if (catalogLoading || catalogUnavailable) return
    setError(null)
    setSubmitting(true)
    start()
  }

  const isUploading = uploadStage === 'transcribing' || uploadStage === 'extracting'

  return (
    <div className="wizard-card">
      <h2>Tell us about your challenge</h2>
      <p className="subtitle">
        Describe what you're hoping an AI team could take off your plate. No technical detail needed — plain
        language is perfect.
      </p>

      {catalogUnavailable && (
        <div className="banner banner-error">
          {catalogFailed
            ? "Couldn't load the available AI models. Check your connection and try again."
            : 'No AI models are available yet. Contact your administrator, or try again.'}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retryCatalog}>
              Try again
            </button>
          </div>
        </div>
      )}

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

      <div className="upload-section">
        <button
          className="btn btn-secondary"
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={submitting || isUploading || catalogLoading || catalogUnavailable}
        >
          {UPLOAD_LABELS[uploadStage ?? ''] ?? (uploadStage === 'done' ? 'Replace recording' : 'Upload interview recording')}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED_AUDIO}
          style={{ display: 'none' }}
          onChange={handleFileUpload}
        />
      </div>

      {uploadError && (
        <div className="banner banner-error" style={{ marginTop: 8 }}>
          {uploadError}
        </div>
      )}

      {transcript && (
        <details className="transcript-section">
          <summary>See full transcript</summary>
          <pre className="transcript-text">{transcript}</pre>
        </details>
      )}

      <div className="or-divider">
        <span>or describe it below</span>
      </div>

      <div className="field">
        <label htmlFor="intent">What do you want help with?</label>
        <textarea
          id="intent"
          rows={5}
          value={intentText}
          onChange={(e) => setIntentText(e.target.value)}
          placeholder="e.g. We get dozens of customer support emails a day and can't keep up with replies."
          disabled={submitting || isUploading}
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
          disabled={submitting || isUploading}
        />
      </div>

      <div className="wizard-actions">
        <button
          className="btn btn-primary"
          onClick={start}
          disabled={!intentText.trim() || submitting || isUploading || catalogLoading || catalogUnavailable}
        >
          {submitting
            ? STAGE_LABELS[stage ?? ''] ?? 'Starting…'
            : catalogLoading
              ? 'Loading available models…'
              : 'Start building my team'}
        </button>
      </div>
    </div>
  )
}
