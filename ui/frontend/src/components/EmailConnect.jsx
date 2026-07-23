import { useEffect, useState } from 'react'
import { api } from '../lib/api'

// Connect / test / rotate / disconnect the org's mailbox for the email tools.
// Shown inside the wizard only when the team uses email (session.uses_email).
// `onChange` fires after a successful connect/disconnect (a parent may clear its
// own error). `onStatusChange` fires with the current connected boolean whenever
// it's known, so the Deploy gate can disable launch until a mailbox is connected.
export default function EmailConnect({ onChange, onStatusChange }) {
  const [status, setStatus] = useState(null) // {connected, host, username, ...} | null
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ host: '', username: '', password: '', port: 993, drafts: '' })
  const [testResult, setTestResult] = useState(null) // {ok, error} | null
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState('') // '' | 'test' | 'save' | 'clear'
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
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const field = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const payload = () => ({
    host: form.host.trim(),
    username: form.username.trim(),
    password: form.password,
    port: Number(form.port) || 993,
    drafts: form.drafts.trim() || null,
  })

  const test = async () => {
    setBusy('test'); setError(null); setTestResult(null)
    try {
      setTestResult(await api.testOrgEmail(payload()))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const save = async () => {
    setBusy('save'); setError(null)
    try {
      await api.setOrgEmail(payload())
      setEditing(false)
      setForm({ host: '', username: '', password: '', port: 993, drafts: '' })
      setTestResult(null)
      await refresh()
      onChange?.()
    } catch (e) {
      setError(e.message)
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
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  // Prefill from the existing connection when reconnecting (password is
  // write-only, so it stays blank).
  const startReconnect = () => {
    setForm({
      host: status.host || '',
      username: status.username || '',
      password: '',
      port: status.port || 993,
      drafts: status.drafts || '',
    })
    // Reveal Advanced if this connection used non-default values, so they're visible.
    setShowAdvanced((status.port && status.port !== 993) || !!status.drafts)
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

  const canSubmit = form.host.trim() && form.username.trim() && form.password

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
