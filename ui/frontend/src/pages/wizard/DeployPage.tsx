import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import EmailBudgetSettings from '../../components/EmailBudgetSettings'
import EmailConnect from '../../components/EmailConnect'
import EmailFilterSettings from '../../components/EmailFilterSettings'
import EmailTriggerToggle from '../../components/EmailTriggerToggle'
import { api } from '../../lib/api'
import type { WizardOutletContext } from '../../lib/types'

export default function DeployPage() {
  const { t } = useTranslation()
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [emailConnected, setEmailConnected] = useState(false)

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  if (!session.specification_json) {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.deploy.title')}</h2>
        <p className="subtitle">{t('wizard.deploy.designFirst')}</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate('/wizard')}>
            {t('common.startOver')}
          </button>
        </div>
      </div>
    )
  }

  const spec = session.specification_json

  const deploy = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const updated = await api.deploySession(sessionId!)
      setSession(updated)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (session.status === 'deployed') {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.deploy.liveTitle')}</h2>
        <p className="banner banner-success">{t('wizard.deploy.liveBody', { name: spec.name })}</p>
        {/* "Live" is true of the deployment, not yet of the work: an email team
            watches nothing until the toggle below is on. */}
        {session.uses_email && <p className="hint">{t('wizard.deploy.liveEmailNext')}</p>}
        {session.uses_email && <EmailConnect />}
        {session.uses_email && <EmailTriggerToggle pipelineName={spec.name} />}
        {/* This team's mail-handling settings live here, not on the Activity
            page's Automations tab -- they configure this team, the same way
            EmailConnect/EmailTriggerToggle above already do. */}
        {session.uses_email && <EmailFilterSettings />}
        {session.uses_email && <EmailBudgetSettings />}
        {/* The two kinds of team leave this screen in different states, so they
            get different next steps. A team that answers questions is finished
            here and the useful thing is to talk to it. An email team is
            deployed but idle -- automatic runs are off by default -- so the
            switch above is what actually starts it, and the button below only
            closes the build. */}
        <div className="wizard-actions">
          {session.uses_email ? (
            <button className="btn btn-primary" onClick={() => navigate('/teams')}>
              {t('wizard.deploy.done')}
            </button>
          ) : (
            <button
              className="btn btn-primary"
              onClick={() => navigate(`/run?pipeline=${encodeURIComponent(spec.name)}`)}
            >
              {t('wizard.deploy.tryIt')}
            </button>
          )}
          <button className="btn btn-secondary" onClick={() => navigate(`/wizard/${sessionId}/confirm`)}>
            {t('wizard.deploy.adjust')}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.deploy.readyTitle')}</h2>
      <p className="subtitle">{t('wizard.deploy.readySubtitle', { name: spec.name })}</p>

      {session.uses_email && (
        <EmailConnect onChange={() => setError(null)} onStatusChange={setEmailConnected} />
      )}

      {error && <p className="banner banner-error">{error}</p>}

      <div className="wizard-actions">
        <button className="btn btn-hero" onClick={deploy} disabled={busy || (session.uses_email && !emailConnected)}>
          {busy ? t('wizard.deploy.launching') : t('wizard.deploy.launch')}
        </button>
      </div>
      {session.uses_email && !emailConnected && (
        <p className="hint">{t('wizard.deploy.connectMailboxFirst')}</p>
      )}
    </div>
  )
}
