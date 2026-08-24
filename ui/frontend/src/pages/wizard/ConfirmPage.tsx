import { useEffect, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import BulletEditor from '../../components/BulletEditor'
import TeamFlow from '../../components/TeamFlow'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { Requirements, WizardOutletContext } from '../../lib/types'

const EMPTY_REQUIREMENTS: Requirements = {
  summary: '',
  pain_points: [],
  goals: [],
  success_criteria: [],
  constraints: [],
  clarifying_questions: [],
}

export default function ConfirmPage() {
  const { session, setSession, loading, sessionId, setNavBusy } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()
  const { t } = useTranslation()
  // Both actions on this page call the Solution Architect internally, but
  // which model runs it is a platform choice the customer never sees or
  // picks (see the admin's `is_default` catalog entry, `pickDefaultModel`) --
  // they only care about the models their own team's agents end up using,
  // which the Architect assigns. Gating on catalogUnavailable/catalogLoading,
  // the same pattern IntentPage/DocumentsPage use, is what keeps this from
  // becoming a dead end when the catalog is empty or failed to load (audit
  // finding F3).
  const {
    loading: catalogLoading,
    failed: catalogFailed,
    entries: catalogEntries,
    retry: retryCatalog,
  } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && catalogEntries.length === 0)
  const catalogNotReady = catalogLoading || catalogUnavailable

  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Expanded by default: this is the understanding the team below was
  // designed from, and collapsed it was effectively never read.
  const [showRequirements, setShowRequirements] = useState(true)
  const [reqDraft, setReqDraft] = useState<Requirements>(EMPTY_REQUIREMENTS)
  const [reqBusy, setReqBusy] = useState(false)
  const [reqError, setReqError] = useState<string | null>(null)
  // Drafted answers to the open clarifying questions, keyed by question
  // index. Reset whenever the requirements regenerate: that round retires or
  // replaces questions, so a stale answer must not survive it.
  const [answerDraft, setAnswerDraft] = useState<Record<number, string>>({})

  useEffect(() => {
    if (session?.requirements_json) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync editable draft when AI (re)generates requirements
      setReqDraft({ ...EMPTY_REQUIREMENTS, ...session.requirements_json })
      setAnswerDraft({})
    }
  }, [session?.requirements_json])

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.confirm.title')}</h2>
        <p className="subtitle">{t('wizard.needMore')}</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            {t('common.startOver')}
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json
  const history = (session.feedback_history ?? []).filter((entry) => entry.stage === 'solution')

  // The page's one action. It carries both of the customer's inputs -- the
  // fields they edited by hand and whatever they described in words -- and
  // the backend updates the understanding and the team from them together.
  // Describing nothing is a legitimate use (they only edited a field, or they
  // just uploaded a document), so the button never depends on the text box.
  const updateTeam = async () => {
    if (catalogNotReady || busy) return
    setBusy(true)
    // Two model calls run behind this one button, so the wait is long enough
    // that a customer will look for something else to do. The whole page --
    // and the step bar above it -- stops taking clicks until it finishes;
    // "Continue to deploy" in particular would otherwise publish the team the
    // Architect is in the middle of replacing.
    setNavBusy(true)
    setError(null)
    // Answers travel only when typed: a blank answer here is not a skip (no
    // skip button on Confirm) -- the question simply stays open.
    const answered = reqDraft.clarifying_questions
      .map((question, i) => ({ question, answer: (answerDraft[i] ?? '').trim() }))
      .filter((qa) => qa.answer)
    try {
      const updated = await api.refineTeam(sessionId!, {
        // Only when there is a summary to edit -- an empty draft would
        // otherwise overwrite a session whose Requirements call failed.
        ...(session.requirements_json ? { requirements: reqDraft } : {}),
        ...(answered.length ? { answers: answered } : {}),
        feedback: feedback.trim(),
        model: pickDefaultModel(catalogEntries),
      })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
      setNavBusy(false)
    }
  }

  const generateRequirements = async () => {
    if (catalogNotReady || reqBusy) return
    setReqBusy(true)
    setReqError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, { model: pickDefaultModel(catalogEntries) })
      setSession(updated)
    } catch (e) {
      setReqError((e as Error).message)
    } finally {
      setReqBusy(false)
    }
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.confirm.adjustTitle')}</h2>
      <p className="subtitle">{t('wizard.confirm.adjustSubtitle')}</p>

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

      {/* The understanding comes first: the team below is derived from it, so
          a customer correcting something should meet the cause before the
          effect. */}
      <button type="button" className="btn-link" onClick={() => setShowRequirements((v) => !v)}>
        {showRequirements
          ? t('wizard.confirm.hideUnderstanding')
          : t('wizard.confirm.showUnderstanding')}
      </button>

      {showRequirements && (
        <div style={{ marginTop: 16 }}>
          {!session.requirements_json ? (
            <div>
              <p className="hint">{t('wizard.confirm.noSummary')}</p>
              {reqError && <p className="banner banner-error">{reqError}</p>}
              <div className="wizard-actions">
                <button
                  className="btn btn-secondary"
                  onClick={generateRequirements}
                  disabled={catalogNotReady || reqBusy || busy}
                >
                  {reqBusy ? t('wizard.confirm.generating') : t('wizard.confirm.generate')}
                </button>
              </div>
            </div>
          ) : (
            <>
              {reqError && <p className="banner banner-error">{reqError}</p>}

              {reqDraft.clarifying_questions.length > 0 && (
                <div className="banner banner-info">
                  <strong>{t('wizard.confirm.clarifyHeading')}</strong>
                  {reqDraft.clarifying_questions.map((q, i) => (
                    <div className="field" key={i} style={{ marginTop: 8 }}>
                      <label htmlFor={`clarify-${i}`}>{q}</label>
                      <textarea
                        id={`clarify-${i}`}
                        rows={2}
                        value={answerDraft[i] ?? ''}
                        onChange={(e) => setAnswerDraft((prev) => ({ ...prev, [i]: e.target.value }))}
                        placeholder={t('wizard.questions.answerPlaceholder')}
                        disabled={busy}
                      />
                    </div>
                  ))}
                  <p className="hint">{t('wizard.confirm.clarifyHint')}</p>
                </div>
              )}

              <div className="field">
                <label htmlFor="summary">{t('wizard.confirm.summaryLabel')}</label>
                <textarea
                  id="summary"
                  rows={3}
                  value={reqDraft.summary}
                  onChange={(e) => setReqDraft({ ...reqDraft, summary: e.target.value })}
                  disabled={busy}
                />
              </div>

              <div className="field">
                <label>{t('wizard.confirm.painPoints')}</label>
                <BulletEditor
                  items={reqDraft.pain_points}
                  onChange={(items) => setReqDraft({ ...reqDraft, pain_points: items })}
                  disabled={busy}
                  placeholder={t('wizard.confirm.painPointsPlaceholder')}
                />
              </div>

              <div className="field">
                <label>{t('wizard.confirm.goals')}</label>
                <BulletEditor
                  items={reqDraft.goals}
                  onChange={(items) => setReqDraft({ ...reqDraft, goals: items })}
                  disabled={busy}
                  placeholder={t('wizard.confirm.goalsPlaceholder')}
                />
              </div>

              <div className="field">
                <label>{t('wizard.confirm.successCriteria')}</label>
                <BulletEditor
                  items={reqDraft.success_criteria}
                  onChange={(items) => setReqDraft({ ...reqDraft, success_criteria: items })}
                  disabled={busy}
                  placeholder={t('wizard.confirm.successPlaceholder')}
                />
              </div>

              <div className="field">
                <label>{t('wizard.confirm.constraints')}</label>
                <BulletEditor
                  items={reqDraft.constraints}
                  onChange={(items) => setReqDraft({ ...reqDraft, constraints: items })}
                  disabled={busy}
                  placeholder={t('wizard.confirm.constraintsPlaceholder')}
                />
              </div>

              {/* No save button of its own: these fields travel with the
                  single Update action below, so an edit can never be left
                  unsaved and can never be thrown away by another button. */}
            </>
          )}
        </div>
      )}

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <h3>{t('wizard.confirm.teamHeading')}</h3>

      {error && <p className="banner banner-error">{error}</p>}

      <TeamFlow specification={spec} />

      {history.length > 0 && (
        <div className="banner banner-info" style={{ marginTop: 16 }}>
          <strong>{t('wizard.confirm.historyHeading')}</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
            {history.map((entry, i) => (
              <li key={i}>{entry.note}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="field" style={{ marginTop: 16 }}>
        <label htmlFor="solution-feedback">
          {t('wizard.confirm.changeLabel')} <span className="hint">{t('wizard.optional')}</span>
        </label>
        <textarea
          id="solution-feedback"
          rows={3}
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder={t('wizard.confirm.changePlaceholder')}
          disabled={busy}
        />
        <button
          type="button"
          className="btn-link"
          style={{ marginTop: 4 }}
          onClick={() => navigate(`/wizard/${sessionId}/documents`)}
          disabled={busy}
        >
          {t('wizard.confirm.uploadLink')}
        </button>
      </div>
      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={updateTeam} disabled={catalogNotReady || busy}>
          {busy ? t('wizard.confirm.updating') : t('wizard.confirm.apply')}
        </button>
      </div>
      {/* An honest single line, not a staged one: this is one request, so the
          page cannot know when the Analyst hands over to the Architect. */}
      {busy && <p className="hint">{t('wizard.confirm.updatingNotice')}</p>}

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <div className="wizard-actions">
        <button
          className="btn btn-secondary"
          onClick={() => navigate(`/wizard/${sessionId}/preview`)}
          disabled={busy}
        >
          {t('wizard.confirm.backToPreview')}
        </button>
        <button
          className="btn btn-primary"
          onClick={() => navigate(`/wizard/${sessionId}/deploy`)}
          disabled={busy}
        >
          {t('wizard.confirm.continueToDeploy')}
        </button>
      </div>
    </div>
  )
}
