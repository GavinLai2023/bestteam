import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import type { EmailTrigger } from '../lib/types'

interface EmailTriggerToggleProps {
  pipelineName: string
}

// Org-level opt-in: run this deployed email team automatically on new mail.
// Off by default; shown on the Deploy page once the team is live.
export default function EmailTriggerToggle({ pipelineName }: EmailTriggerToggleProps) {
  const { t } = useTranslation()
  const [trigger, setTrigger] = useState<EmailTrigger | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getEmailTrigger().then(setTrigger).catch((e: Error) => setError(e.message))
  }, [])

  if (!trigger) return error ? <p className="banner banner-error">{error}</p> : null

  const onForThis = trigger.enabled && trigger.pipeline_name === pipelineName

  const toggle = async () => {
    setBusy(true)
    setError(null)
    try {
      setTrigger(await api.setEmailTrigger({ pipeline_name: pipelineName, enabled: !onForThis }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wizard-card" style={{ background: '#f9fafb' }}>
      <h3>{t('email.trigger.title')}</h3>
      <p className="subtitle">
        {t('email.trigger.subtitle', { name: pipelineName, cap: trigger.daily_cap })}
      </p>

      {error && <p className="banner banner-error">{error}</p>}
      {/* The trigger row is unique per org, so enabling it here overwrites
          whichever team holds it -- silently, server-side. Name that team. */}
      {trigger.enabled && !onForThis && (
        <p className="banner banner-error">
          {t('email.trigger.takenByOtherTeam', { name: trigger.pipeline_name })}
        </p>
      )}
      {onForThis && trigger.status === 'paused_cap' && (
        <p className="banner banner-error">
          {t('email.trigger.pausedCap', { cap: trigger.daily_cap })}
        </p>
      )}
      {onForThis && trigger.status === 'error' && trigger.last_error && (
        <p className="banner banner-error">{trigger.last_error}</p>
      )}

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={toggle} disabled={busy}>
          {busy
            ? t('email.trigger.saving')
            : t(onForThis ? 'email.trigger.turnOff' : 'email.trigger.turnOn')}
        </button>
        {onForThis && trigger.status === 'active' && (
          <span className="hint">{t('email.trigger.watching')}</span>
        )}
      </div>
    </div>
  )
}
