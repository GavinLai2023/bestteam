import { useEffect, useState } from 'react'
import { api } from '../lib/api'

// Connect / test / rotate / disconnect the org's mailbox for the email tools.
// Shown inside the wizard only when the team uses email (session.uses_email).
// `onChange` is called after a successful connect/disconnect so a parent (the
// Deploy gate) can re-check whether a mailbox is now connected.
export default function EmailConnect({ onChange }) {
  const [status, setStatus] = useState(null) // {connected, host, username, ...} | null (loading)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ host: '', username: '', password: '', port: 993, drafts: '' })
  const [testResult, setTestResult] = useState(null) // {ok, error} | null
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState('') // '' | 'test' | 'save' | 'clear'

  const refresh = () => api.getOrgEmail().then(setStatus).catch((e) => setError(e.message))
  useEffect(() => { refresh() }, [])

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

  if (status === null) return <p className="hint">Checking mailbox…</p>

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
            <button className="btn btn-secondary" onClick={() => setEditing(true)}>Reconnect</button>
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

          {testResult && (
            <p className={`banner ${testResult.ok ? 'banner-success' : 'banner-error'}`}>
              {testResult.ok ? 'Connection works.' : `Couldn't connect: ${testResult.error}`}
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
