import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { WizardOutletContext } from '../../lib/types'

// The interview step: the analyst's clarifying questions, one input each.
// Skipping is a first-class action -- the platform promises "intent in, best
// AI team out", so every question here is work pushed back onto the customer,
// and a skipped question makes the analyst record the assumption it made
// instead (visible in the summary's constraints).
export default function QuestionsPage() {
  const { t } = useTranslation()
  const { session, setSession, loading, sessionId, setNavBusy } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)

  const [answers, setAnswers] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Which action failed, so "Try again" repeats it: retrying a failed Skip
  // must not quietly turn typed-but-skipped answers into a Continue.
  const [lastSkip, setLastSkip] = useState(false)

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  const questions = session.requirements_json?.clarifying_questions ?? []
  const anyAnswered = questions.some((_, i) => (answers[i] ?? '').trim())

  // Only reachable by revisiting: IntentPage skips this step when the
  // analyst had nothing to ask, and answering retires the questions.
  if (questions.length === 0) {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.questions.title')}</h2>
        <p className="subtitle">{t('wizard.questions.noQuestions')}</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/documents`)}>
            {t('common.continue')}
          </button>
        </div>
      </div>
    )
  }

  // One call for both buttons: a skip is the same request with every answer
  // blank, and the analyst records the assumptions it made instead.
  const submit = async (skip: boolean) => {
    if (busy || catalogLoading || catalogUnavailable) return
    setLastSkip(skip)
    setBusy(true)
    // The analyst call is long enough for the customer to wander; suspend the
    // step bar like the Confirm page does while its one action is in flight.
    setNavBusy(true)
    setError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, {
        model: pickDefaultModel(entries),
        answers: questions.map((question, i) => ({
          question,
          answer: skip ? '' : (answers[i] ?? '').trim(),
        })),
      })
      setSession(updated)
      navigate(`/wizard/${sessionId}/documents`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
      setNavBusy(false)
    }
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.questions.title')}</h2>
      <p className="subtitle">{t('wizard.questions.subtitle')}</p>

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
            <button className="btn btn-secondary" onClick={() => submit(lastSkip)} disabled={busy}>
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      {questions.map((question, i) => (
        <div className="field" key={i}>
          <label htmlFor={`question-${i}`}>{question}</label>
          <textarea
            id={`question-${i}`}
            rows={2}
            value={answers[i] ?? ''}
            onChange={(e) => setAnswers((prev) => ({ ...prev, [i]: e.target.value }))}
            placeholder={t('wizard.questions.answerPlaceholder')}
            disabled={busy}
          />
        </div>
      ))}

      <div className="wizard-actions">
        <button
          className="btn btn-secondary"
          onClick={() => submit(true)}
          disabled={busy || catalogLoading || catalogUnavailable}
        >
          {t('wizard.questions.skip')}
        </button>
        <button
          className="btn btn-primary"
          onClick={() => submit(false)}
          disabled={busy || catalogLoading || catalogUnavailable || !anyAnswered}
        >
          {busy ? t('wizard.questions.updating') : t('common.continue')}
        </button>
      </div>
      {busy && <p className="hint">{t('wizard.questions.updatingNotice')}</p>}
    </div>
  )
}
