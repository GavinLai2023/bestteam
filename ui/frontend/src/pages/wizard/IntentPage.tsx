import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'

const ACCEPTED_AUDIO = '.mp3,.mp4,.m4a,.wav,.webm,.mpeg,.mpga'

type Stage = null | 'creating' | 'requirements'
type UploadStage = null | 'transcribing' | 'extracting' | 'done'

export default function IntentPage() {
  const { t } = useTranslation()
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
    // intent/as-is text) if this fails, so don't block on it. When the
    // analyst has questions, the interview step comes before any documents.
    setStage('requirements')
    let next = `/wizard/${id}/documents`
    try {
      const updated = await api.submitRequirements(id, { model })
      if (updated.requirements_json?.clarifying_questions?.length) {
        next = `/wizard/${id}/questions`
      }
    } catch {
      // ignored — non-blocking
    }

    navigate(next)
  }

  const retry = () => {
    if (catalogLoading || catalogUnavailable) return
    setError(null)
    setSubmitting(true)
    start()
  }

  const isUploading = uploadStage === 'transcribing' || uploadStage === 'extracting'

  const submitLabel = () => {
    if (stage === 'creating') return t('wizard.intent.creating')
    if (stage === 'requirements') return t('wizard.intent.requirements')
    return t('wizard.intent.starting')
  }

  const uploadLabel = () => {
    if (uploadStage === 'transcribing') return t('wizard.intent.transcribing')
    if (uploadStage === 'extracting') return t('wizard.intent.extracting')
    if (uploadStage === 'done') return t('wizard.intent.replaceRecording')
    return t('wizard.intent.uploadRecording')
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.intent.title')}</h2>
      <p className="subtitle">{t('wizard.intent.subtitle')}</p>

      {catalogUnavailable && (
        <div className="banner banner-error">
          {catalogFailed ? t('modelCatalog.loadFailed') : t('modelCatalog.empty')}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retryCatalog}>
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="banner banner-error">
          {error}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retry} disabled={submitting}>
              {t('common.tryAgain')}
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
          {uploadLabel()}
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
          <summary>{t('wizard.intent.seeTranscript')}</summary>
          <pre className="transcript-text">{transcript}</pre>
        </details>
      )}

      <div className="or-divider">
        <span>{t('wizard.intent.orDescribe')}</span>
      </div>

      <div className="field">
        <label htmlFor="intent">{t('wizard.intent.intentLabel')}</label>
        <textarea
          id="intent"
          rows={5}
          value={intentText}
          onChange={(e) => setIntentText(e.target.value)}
          placeholder={t('wizard.intent.intentPlaceholder')}
          disabled={submitting || isUploading}
        />
      </div>

      <div className="field">
        <label htmlFor="as-is">
          {t('wizard.intent.asIsLabel')} <span className="hint">{t('wizard.optional')}</span>
        </label>
        <textarea
          id="as-is"
          rows={4}
          value={asIsText}
          onChange={(e) => setAsIsText(e.target.value)}
          placeholder={t('wizard.intent.asIsPlaceholder')}
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
            ? submitLabel()
            : catalogLoading
              ? t('wizard.intent.loadingModels')
              : t('wizard.intent.start')}
        </button>
      </div>
    </div>
  )
}
