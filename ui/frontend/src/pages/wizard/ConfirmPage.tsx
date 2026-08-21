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
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
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

  const [showRequirements, setShowRequirements] = useState(false)
  const [reqDraft, setReqDraft] = useState<Requirements>(EMPTY_REQUIREMENTS)
  const [reqFeedback, setReqFeedback] = useState('')
  const [reqBusy, setReqBusy] = useState(false)
  const [reqError, setReqError] = useState<string | null>(null)

  useEffect(() => {
    if (session?.requirements_json) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- sync editable draft when AI (re)generates requirements
      setReqDraft({ ...EMPTY_REQUIREMENTS, ...session.requirements_json })
    }
  }, [session?.requirements_json])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>Confirm your team</h2>
        <p className="subtitle">We need a bit more information first.</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            Start over
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json
  const history = (session.feedback_history ?? []).filter((entry) => entry.stage === 'solution')

  const applyFeedback = async () => {
    // Feedback is optional -- an empty description still re-runs the
    // Architect (e.g. after uploading new documents).
    if (catalogNotReady || busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.submitSolution(sessionId!, {
        feedback: feedback.trim(),
        model: pickDefaultModel(catalogEntries),
      })
      setSession(updated)
      setFeedback('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const saveRequirements = async () => {
    if (reqBusy) return
    setReqBusy(true)
    setReqError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, { requirements: reqDraft })
      setSession(updated)
    } catch (e) {
      setReqError((e as Error).message)
    } finally {
      setReqBusy(false)
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

  const regenerateRequirements = async () => {
    if (catalogNotReady || !reqFeedback.trim() || reqBusy) return
    setReqBusy(true)
    setReqError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, {
        model: pickDefaultModel(catalogEntries),
        feedback: reqFeedback.trim(),
      })
      setSession(updated)
      setReqFeedback('')
    } catch (e) {
      setReqError((e as Error).message)
    } finally {
      setReqBusy(false)
    }
  }

  return (
    <div className="wizard-card">
      <h2>Anything to adjust?</h2>
      <p className="subtitle">
        Here's your team again. If something's off, describe the change and we'll redesign it — you can always go
        back to try it out again.
      </p>

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
        <button
          type="button"
          className="btn-link"
          style={{ marginTop: 4 }}
          onClick={() => navigate(`/wizard/${sessionId}/documents`)}
        >
          Need to add or update a document? Upload it here
        </button>
      </div>
      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={applyFeedback} disabled={catalogNotReady || busy}>
          {busy ? 'Updating…' : 'Apply this change'}
        </button>
      </div>

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <button type="button" className="btn-link" onClick={() => setShowRequirements((v) => !v)}>
        {showRequirements ? 'Hide' : 'Show'} what we understood about your business
      </button>

      {showRequirements && (
        <div style={{ marginTop: 16 }}>
          {!session.requirements_json ? (
            <div>
              <p className="hint">No summary was generated for this session.</p>
              {reqError && <p className="banner banner-error">{reqError}</p>}
              <div className="wizard-actions">
                <button
                  className="btn btn-secondary"
                  onClick={generateRequirements}
                  disabled={catalogNotReady || reqBusy}
                >
                  {reqBusy ? 'Generating…' : 'Generate summary'}
                </button>
              </div>
            </div>
          ) : (
            <>
              {reqError && <p className="banner banner-error">{reqError}</p>}

              {reqDraft.clarifying_questions.length > 0 && (
                <div className="banner banner-info">
                  <strong>A couple of quick questions:</strong>
                  <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                    {reqDraft.clarifying_questions.map((q, i) => (
                      <li key={i}>{q}</li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="field">
                <label htmlFor="summary">Summary</label>
                <textarea
                  id="summary"
                  rows={3}
                  value={reqDraft.summary}
                  onChange={(e) => setReqDraft({ ...reqDraft, summary: e.target.value })}
                />
              </div>

              <div className="field">
                <label>Pain points</label>
                <BulletEditor
                  items={reqDraft.pain_points}
                  onChange={(items) => setReqDraft({ ...reqDraft, pain_points: items })}
                  placeholder="e.g. replies take too long"
                />
              </div>

              <div className="field">
                <label>Goals</label>
                <BulletEditor
                  items={reqDraft.goals}
                  onChange={(items) => setReqDraft({ ...reqDraft, goals: items })}
                  placeholder="e.g. reply within an hour"
                />
              </div>

              <div className="field">
                <label>What does success look like?</label>
                <BulletEditor
                  items={reqDraft.success_criteria}
                  onChange={(items) => setReqDraft({ ...reqDraft, success_criteria: items })}
                  placeholder="e.g. fewer escalations"
                />
              </div>

              <div className="field">
                <label>Constraints</label>
                <BulletEditor
                  items={reqDraft.constraints}
                  onChange={(items) => setReqDraft({ ...reqDraft, constraints: items })}
                  placeholder="e.g. must stay in English"
                />
              </div>

              <div className="wizard-actions">
                <button className="btn btn-secondary" onClick={saveRequirements} disabled={reqBusy}>
                  {reqBusy ? 'Saving…' : 'Save changes'}
                </button>
              </div>

              <div className="field" style={{ marginTop: 12 }}>
                <label htmlFor="req-feedback">
                  Not quite right? Tell us what to change <span className="hint">(optional)</span>
                </label>
                <textarea
                  id="req-feedback"
                  rows={2}
                  value={reqFeedback}
                  onChange={(e) => setReqFeedback(e.target.value)}
                  placeholder="e.g. We also use Zendesk for tickets, and replies must stay under 150 words."
                />
              </div>
              <div className="wizard-actions">
                <button
                  className="btn btn-secondary"
                  onClick={regenerateRequirements}
                  disabled={catalogNotReady || !reqFeedback.trim() || reqBusy}
                >
                  {reqBusy ? 'Thinking…' : 'Regenerate summary'}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      <hr style={{ margin: '24px 0', border: 'none', borderTop: '1px solid #e5e7eb' }} />

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={() => navigate(`/wizard/${sessionId}/preview`)}>
          Back to preview
        </button>
        <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/deploy`)}>
          Continue to deploy
        </button>
      </div>
    </div>
  )
}
