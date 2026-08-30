import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { useConfirm } from '../lib/useConfirm'
import type { OrgEmailAuthType, OrgEmailConnectPayload, OrgEmailStatus } from '../lib/types'

interface EmailConnectProps {
  onChange?: () => void
  onStatusChange?: (connected: boolean) => void
}

interface EmailForm {
  authType: OrgEmailAuthType
  host: string
  username: string
  password: string
  tenantId: string
  clientId: string
  clientSecret: string
  // ISO date (YYYY-MM-DD). Optional, Microsoft 365 only.
  secretExpiresAt: string
  port: number | string
  drafts: string
}

const EMPTY_FORM: EmailForm = {
  authType: 'password', host: '', username: '', password: '',
  tenantId: '', clientId: '', clientSecret: '', secretExpiresAt: '',
  port: 993, drafts: '',
}

// Connect / test / rotate / disconnect the org's mailbox for the email tools.
// Shown inside the wizard only when the team uses email (session.uses_email).
// `onChange` fires after a successful connect/disconnect (a parent may clear its
// own error). `onStatusChange` fires with the current connected boolean whenever
// it's known, so the Deploy gate can disable launch until a mailbox is connected.
export default function EmailConnect({ onChange, onStatusChange }: EmailConnectProps) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<OrgEmailStatus | null>(null) // {connected, host, username, ...} | null
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<EmailForm>(EMPTY_FORM)
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'' | 'test' | 'save' | 'clear'>('')
  // Port + drafts live under "Advanced settings": a non-technical user (Gmail,
  // Outlook) never needs them, and a visible number field invites the
  // scroll-wheel-changes-993-to-994 mistake.
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [confirmNode, confirm] = useConfirm()

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await api.getOrgEmail()
      setStatus(s)
      onStatusChange?.(!!s.connected)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const field = (k: keyof EmailForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value })

  const isM365 = form.authType === 'microsoft_oauth'

  const payload = (): OrgEmailConnectPayload => ({
    auth_type: form.authType,
    // Fixed server-side for Microsoft 365; sending '' says so honestly.
    host: isM365 ? '' : form.host.trim(),
    username: form.username.trim(),
    password: isM365 ? null : form.password,
    client_secret: isM365 ? form.clientSecret : null,
    oauth_tenant_id: isM365 ? form.tenantId.trim() : null,
    oauth_client_id: isM365 ? form.clientId.trim() : null,
    oauth_secret_expires_at: isM365 ? form.secretExpiresAt.trim() || null : null,
    port: Number(form.port) || 993,
    drafts: form.drafts.trim() || null,
  })

  const test = async () => {
    setBusy('test'); setError(null); setTestResult(null)
    try {
      setTestResult(await api.testOrgEmail(payload()))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const save = async () => {
    // A changed address replaces the org's one mailbox for every team and
    // switches automatic runs off server-side (`on_mailbox_saved`). The banner
    // above says so; this makes it an answer rather than something to notice.
    const target = form.username.trim()
    if (status?.connected && target !== (status.username || '')) {
      const ok = await confirm({
        title: t('email.connect.switchConfirmTitle'),
        body: t('email.connect.switchConfirmBody', {
          current: status.username, address: target,
        }),
        confirmLabel: t('email.connect.switchConfirmAction'),
        destructive: true,
      })
      if (!ok) return
    }
    setBusy('save'); setError(null)
    try {
      await api.setOrgEmail(payload())
      setEditing(false)
      setForm(EMPTY_FORM)
      setTestResult(null)
      await refresh()
      onChange?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const disconnect = async () => {
    setBusy('clear'); setError(null)
    try {
      await api.clearOrgEmail()
      await refresh()
      onChange?.()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  // Prefill from the existing connection when reconnecting (password is
  // write-only, so it stays blank).
  const startReconnect = () => {
    if (!status) return
    setForm({
      ...EMPTY_FORM,
      authType: status.auth_type || 'password',
      host: status.host || '',
      username: status.username || '',
      tenantId: status.oauth_tenant_id || '',
      clientId: status.oauth_client_id || '',
      secretExpiresAt: status.oauth_secret_expires_at || '',
      port: status.port || 993,
      drafts: status.drafts || '',
    })
    // Reveal Advanced if this connection used non-default values, so they're visible.
    setShowAdvanced(Boolean((status.port && status.port !== 993) || status.drafts))
    setEditing(true)
  }

  if (loading) return <p className="hint">{t('email.connect.checking')}</p>

  // Initial status fetch failed: show the error and let them retry, rather than
  // sitting on "Checking mailbox…" forever.
  if (status === null) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb' }}>
        <h3>{t('email.connect.title')}</h3>
        {error && <p className="banner banner-error">{error}</p>}
        <div className="wizard-actions">
          <button className="btn btn-secondary" onClick={refresh}>{t('email.connect.retry')}</button>
        </div>
      </div>
    )
  }

  const canSubmit = isM365
    ? form.username.trim() && form.tenantId.trim() && form.clientId.trim() && form.clientSecret
    : form.host.trim() && form.username.trim() && form.password

  return (
    <div className="wizard-card" style={{ background: '#f9fafb' }}>
      {confirmNode}
      <h3>{t('email.connect.title')}</h3>
      <p className="subtitle">{t('email.connect.subtitle')}</p>

      {error && <p className="banner banner-error">{error}</p>}

      {status.connected && !editing ? (
        <>
          <p className="banner banner-success">
            {t('email.connect.connectedPrefix')}
            <strong>{status.username}</strong>
            {t('email.connect.connectedSuffix', { host: status.host })}
          </p>
          {/* The mailbox is org-wide (one row per org), so a second team built
              later reconnects THIS mailbox rather than adding one. Say so, or
              switching it looks like a per-team setting. */}
          <p className="hint">{t('email.connect.sharedByEveryTeam')}</p>
          <div className="wizard-actions">
            <button className="btn btn-secondary" onClick={startReconnect}>
              {t('email.connect.reconnect')}
            </button>
            <button className="btn btn-link" onClick={disconnect} disabled={busy === 'clear'}>
              {t(busy === 'clear' ? 'email.connect.disconnecting' : 'email.connect.disconnect')}
            </button>
          </div>
        </>
      ) : (
        <>
          <fieldset className="field">
            <legend>{t('email.connect.hostingLegend')}</legend>
            <label htmlFor="ec-auth-password">
              <input
                id="ec-auth-password" type="radio" name="ec-auth" value="password"
                checked={!isM365}
                onChange={() => setForm({ ...form, authType: 'password' })}
              />{' '}
              {t('email.connect.hostingImap')}
            </label>
            <label htmlFor="ec-auth-m365">
              <input
                id="ec-auth-m365" type="radio" name="ec-auth" value="microsoft_oauth"
                checked={isM365}
                onChange={() => setForm({ ...form, authType: 'microsoft_oauth' })}
              />{' '}
              {t('email.connect.hostingMicrosoft')}
            </label>
          </fieldset>

          {isM365 ? (
            <>
              <p className="hint">
                {t('email.connect.microsoftHintBefore')}
                <strong>IMAP.AccessAsApp</strong>
                {t('email.connect.microsoftHintAfter')}
              </p>
              <div className="field">
                <label htmlFor="ec-user">{t('email.connect.emailAddress')}</label>
                <input id="ec-user" type="text" value={form.username} onChange={field('username')} placeholder="support@yourcompany.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-tenant">{t('email.connect.tenantId')}</label>
                <input id="ec-tenant" type="text" value={form.tenantId} onChange={field('tenantId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-client">{t('email.connect.clientId')}</label>
                <input id="ec-client" type="text" value={form.clientId} onChange={field('clientId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-secret">{t('email.connect.clientSecret')}</label>
                <input id="ec-secret" type="password" value={form.clientSecret} onChange={field('clientSecret')} autoComplete="off" />
              </div>
              <div className="field">
                <label htmlFor="ec-secret-expiry">{t('email.connect.secretExpiry')}</label>
                <input id="ec-secret-expiry" type="date" value={form.secretExpiresAt} onChange={field('secretExpiresAt')} />
                <p className="hint">{t('email.connect.secretExpiryHint')}</p>
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label htmlFor="ec-host">{t('email.connect.imapServer')}</label>
                <input id="ec-host" type="text" value={form.host} onChange={field('host')} placeholder="imap.gmail.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-user">{t('email.connect.imapUsername')}</label>
                <input id="ec-user" type="text" value={form.username} onChange={field('username')} placeholder="you@example.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-pass">{t('email.connect.appPassword')}</label>
                <input id="ec-pass" type="password" value={form.password} onChange={field('password')} autoComplete="off" />
              </div>
            </>
          )}

          <button
            type="button"
            className="btn btn-link"
            style={{ padding: 0, alignSelf: 'flex-start' }}
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {t(showAdvanced ? 'email.connect.advancedHide' : 'email.connect.advancedShow')}
          </button>

          {showAdvanced && (
            <>
              {/* Exchange Online is always 993, so there is nothing to choose. */}
              {!isM365 && (
              <div className="field">
                <label htmlFor="ec-port">{t('email.connect.port')}</label>
                {/* type=number changes on mouse-wheel scroll; blur on wheel so a
                    non-technical user can't silently turn 993 into 994. */}
                <input
                  id="ec-port"
                  type="number"
                  min={1}
                  max={65535}
                  value={form.port}
                  onChange={field('port')}
                  onWheel={(e) => e.currentTarget.blur()}
                  placeholder="993"
                />
                <p className="hint">{t('email.connect.portHint')}</p>
              </div>
              )}
              <div className="field">
                <label htmlFor="ec-drafts">{t('email.connect.draftsFolder')}</label>
                <input id="ec-drafts" type="text" value={form.drafts} onChange={field('drafts')} placeholder={t('email.connect.draftsPlaceholder')} />
                <p className="hint">{t('email.connect.draftsHint')}</p>
              </div>
            </>
          )}

          {/* Changing the address (not just the password) is a replacement:
              the server switches every team over and turns automatic runs off
              (`on_mailbox_saved`). Warn before the save, not after. */}
          {status.connected && form.username.trim() &&
            form.username.trim() !== (status.username || '') && (
            <p className="banner banner-error">
              {t('email.connect.switchWarning', { address: form.username.trim() })}
            </p>
          )}

          {testResult && (
            <p className={`banner ${testResult.ok ? 'banner-success' : 'banner-error'}`}>
              {testResult.ok ? t('email.connect.testOk') : testResult.error}
            </p>
          )}

          <div className="wizard-actions">
            <button className="btn btn-secondary" onClick={test} disabled={!canSubmit || busy === 'test'}>
              {t(busy === 'test' ? 'email.connect.testing' : 'email.connect.test')}
            </button>
            <button className="btn btn-primary" onClick={save} disabled={!canSubmit || busy === 'save'}>
              {t(busy === 'save' ? 'email.connect.saving' : 'email.connect.save')}
            </button>
            {status.connected && (
              <button className="btn btn-link" onClick={() => { setEditing(false); setTestResult(null) }}>{t('common.cancel')}</button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
