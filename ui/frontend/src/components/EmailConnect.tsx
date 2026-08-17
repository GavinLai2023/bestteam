import { useEffect, useState } from 'react'
import { api } from '../lib/api'
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
  port: number | string
  drafts: string
}

const EMPTY_FORM: EmailForm = {
  authType: 'password', host: '', username: '', password: '',
  tenantId: '', clientId: '', clientSecret: '', port: 993, drafts: '',
}

// Connect / test / rotate / disconnect the org's mailbox for the email tools.
// Shown inside the wizard only when the team uses email (session.uses_email).
// `onChange` fires after a successful connect/disconnect (a parent may clear its
// own error). `onStatusChange` fires with the current connected boolean whenever
// it's known, so the Deploy gate can disable launch until a mailbox is connected.
export default function EmailConnect({ onChange, onStatusChange }: EmailConnectProps) {
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
      port: status.port || 993,
      drafts: status.drafts || '',
    })
    // Reveal Advanced if this connection used non-default values, so they're visible.
    setShowAdvanced(Boolean((status.port && status.port !== 993) || status.drafts))
    setEditing(true)
  }

  if (loading) return <p className="hint">Checking mailbox…</p>

  // Initial status fetch failed: show the error and let them retry, rather than
  // sitting on "Checking mailbox…" forever.
  if (status === null) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb' }}>
        <h3>Connect your mailbox</h3>
        {error && <p className="banner banner-error">{error}</p>}
        <div className="wizard-actions">
          <button className="btn btn-secondary" onClick={refresh}>Retry</button>
        </div>
      </div>
    )
  }

  const canSubmit = isM365
    ? form.username.trim() && form.tenantId.trim() && form.clientId.trim() && form.clientSecret
    : form.host.trim() && form.username.trim() && form.password

  return (
    <div className="wizard-card" style={{ background: '#f9fafb' }}>
      <h3>Connect your mailbox</h3>
      <p className="subtitle">
        This team reads and drafts email in your inbox. It only ever saves <strong>drafts</strong> for
        you to review — it never sends. Use an app-specific password, not your account password.
      </p>

      {error && <p className="banner banner-error">{error}</p>}

      {status.connected && !editing ? (
        <>
          <p className="banner banner-success">
            Connected as <strong>{status.username}</strong> on {status.host}.
          </p>
          <div className="wizard-actions">
            <button className="btn btn-secondary" onClick={startReconnect}>Reconnect</button>
            <button className="btn btn-link" onClick={disconnect} disabled={busy === 'clear'}>
              {busy === 'clear' ? 'Disconnecting…' : 'Disconnect'}
            </button>
          </div>
        </>
      ) : (
        <>
          <fieldset className="field">
            <legend>How is this mailbox hosted?</legend>
            <label htmlFor="ec-auth-password">
              <input
                id="ec-auth-password" type="radio" name="ec-auth" value="password"
                checked={!isM365}
                onChange={() => setForm({ ...form, authType: 'password' })}
              />{' '}
              Standard mailbox (IMAP) — Gmail, and most providers
            </label>
            <label htmlFor="ec-auth-m365">
              <input
                id="ec-auth-m365" type="radio" name="ec-auth" value="microsoft_oauth"
                checked={isM365}
                onChange={() => setForm({ ...form, authType: 'microsoft_oauth' })}
              />{' '}
              Microsoft 365 / Outlook (Exchange Online)
            </label>
          </fieldset>

          {isM365 ? (
            <>
              <p className="hint">
                Microsoft 365 no longer allows app passwords, so this connects through an
                app registration instead. Ask your IT administrator to register an app in
                Azure, grant it the <strong>IMAP.AccessAsApp</strong> permission with admin
                consent, and give it access to this mailbox in Exchange Online. They will
                then have the three values below.
              </p>
              <div className="field">
                <label htmlFor="ec-user">Email address</label>
                <input id="ec-user" type="text" value={form.username} onChange={field('username')} placeholder="support@yourcompany.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-tenant">Directory (tenant) ID</label>
                <input id="ec-tenant" type="text" value={form.tenantId} onChange={field('tenantId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-client">Application (client) ID</label>
                <input id="ec-client" type="text" value={form.clientId} onChange={field('clientId')} />
              </div>
              <div className="field">
                <label htmlFor="ec-secret">Client secret</label>
                <input id="ec-secret" type="password" value={form.clientSecret} onChange={field('clientSecret')} autoComplete="off" />
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label htmlFor="ec-host">IMAP server</label>
                <input id="ec-host" type="text" value={form.host} onChange={field('host')} placeholder="imap.gmail.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-user">Email address / username</label>
                <input id="ec-user" type="text" value={form.username} onChange={field('username')} placeholder="you@example.com" />
              </div>
              <div className="field">
                <label htmlFor="ec-pass">App password</label>
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
            {showAdvanced ? '▾ Advanced settings' : '▸ Advanced settings'}
          </button>

          {showAdvanced && (
            <>
              {/* Exchange Online is always 993, so there is nothing to choose. */}
              {!isM365 && (
              <div className="field">
                <label htmlFor="ec-port">IMAP port</label>
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
                <p className="hint">Almost always 993 — leave as-is unless your email provider says otherwise.</p>
              </div>
              )}
              <div className="field">
                <label htmlFor="ec-drafts">Drafts folder</label>
                <input id="ec-drafts" type="text" value={form.drafts} onChange={field('drafts')} placeholder="Leave blank" />
                <p className="hint">Leave blank — we'll find your Drafts folder automatically. Only set this to force a specific folder.</p>
              </div>
            </>
          )}

          {testResult && (
            <p className={`banner ${testResult.ok ? 'banner-success' : 'banner-error'}`}>
              {testResult.ok ? 'Connection works.' : testResult.error}
            </p>
          )}

          <div className="wizard-actions">
            <button className="btn btn-secondary" onClick={test} disabled={!canSubmit || busy === 'test'}>
              {busy === 'test' ? 'Testing…' : 'Test connection'}
            </button>
            <button className="btn btn-primary" onClick={save} disabled={!canSubmit || busy === 'save'}>
              {busy === 'save' ? 'Connecting…' : 'Connect mailbox'}
            </button>
            {status.connected && (
              <button className="btn btn-link" onClick={() => { setEditing(false); setTestResult(null) }}>Cancel</button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
