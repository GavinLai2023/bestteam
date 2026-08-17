import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { NotificationSettings } from '../lib/types'

// Where the org wants alerts delivered, on top of the in-app list. Optional:
// leaving the URL blank keeps alerts in-app only, which is a valid setup, not
// an unfinished one.
export default function WebhookSettings() {
  const [settings, setSettings] = useState<NotificationSettings | null>(null)
  const [url, setUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.getNotificationSettings()
        setSettings(data)
        setUrl(data.webhook_url || '')
        setEnabled(data.enabled)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const data = await api.setNotificationSettings({
        webhook_url: url.trim() || null,
        // Omitted when untouched so the stored secret survives -- we never
        // received it, so we cannot send it back.
        ...(secret === '' ? {} : { webhook_secret: secret }),
        enabled,
      })
      setSettings(data)
      setSecret('')
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="muted">Loading alert settings&hellip;</p>

  return (
    <section className="webhook-settings">
      <h3>Where to send alerts</h3>
      <p className="hint">
        Alerts always appear here in the app. If you also want them in Slack, Teams
        or an on-call system, paste that service&rsquo;s incoming webhook address
        below. We send only what went wrong &mdash; never the contents of your email.
      </p>

      <div className="field">
        <label htmlFor="wh-url">Webhook address (optional)</label>
        <input
          id="wh-url"
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://hooks.example.com/services/..."
        />
        <p className="hint">Must start with https://.</p>
      </div>

      <div className="field">
        <label htmlFor="wh-secret">
          Signing secret (optional{settings?.has_webhook_secret ? ', already set' : ''})
        </label>
        <input
          id="wh-secret"
          type="password"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          autoComplete="off"
          placeholder={settings?.has_webhook_secret ? 'Leave blank to keep the current secret' : ''}
        />
        <p className="hint">
          If set, each delivery carries an <code>X-BestTeam-Signature</code> header so
          the receiving service can confirm the alert really came from here.
        </p>
      </div>

      <label>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />{' '}
        Send alerts to this address
      </label>

      {error && <p className="error">{error}</p>}
      {saved && !error && <p className="muted">Saved.</p>}

      <button type="button" onClick={() => void save()} disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </button>
    </section>
  )
}
