import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { OrgExportBundle, RetentionSettings } from '../lib/types'

// null = keep forever, and it is the default: nothing is ever removed until
// the customer picks a period.
const PERIODS: { value: number | null; label: string }[] = [
  { value: null, label: 'Keep forever' },
  { value: 30, label: '30 days' },
  { value: 90, label: '90 days' },
  { value: 180, label: '180 days' },
  { value: 365, label: '365 days' },
]

const CONFIRM_WORD = 'DELETE'

// "1 run" / "3 runs" -- a count with a correctly pluralised noun.
function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

// The Activity page's Data tab: how long run history is kept, a copy of it to
// take away first, and removing it on demand.
//
// Every sentence here has to be literally true -- this is the screen where
// somebody decides to delete their own data. A cleanup removes CONTENT (the
// message text, our drafted reply, the step-by-step trace) and keeps
// ACCOUNTING (that the run happened, when, and what it cost), which is
// exactly what ui/backend/retention.py does. It cannot erase one
// correspondent's messages, so nothing here implies it can.
export default function DataRetentionPanel() {
  const [settings, setSettings] = useState<RetentionSettings | null>(null)
  const [days, setDays] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exported, setExported] = useState<OrgExportBundle | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [purging, setPurging] = useState(false)
  const [purged, setPurged] = useState<number | null>(null)
  const [purgeError, setPurgeError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.getRetention()
        setSettings(data)
        setDays(data.run_retention_days)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const save = async () => {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const data = await api.setRetention(days)
      setSettings(data)
      setDays(data.run_retention_days)
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const download = async () => {
    setExporting(true)
    setExportError(null)
    setExported(null)
    try {
      setExported(await api.exportOrgData())
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  const purge = async () => {
    if (days === null) return
    setPurging(true)
    setPurgeError(null)
    setPurged(null)
    try {
      const result = await api.purgeRuns(days)
      setPurged(result.purged)
      setConfirmation('')
      // Re-read rather than adjust locally: `purgeable_now` is the server's
      // count, and leaving a stale one on this screen would be a lie.
      setSettings(await api.getRetention())
    } catch (e) {
      setPurgeError(e instanceof Error ? e.message : String(e))
    } finally {
      setPurging(false)
    }
  }

  if (loading) return <p className="muted">Loading your data settings&hellip;</p>

  const unsaved = settings !== null && days !== settings.run_retention_days

  return (
    <section className="data-retention-panel">
      <h3>Your data</h3>
      <p className="hint">
        We remove what was in the email &mdash; the message text, the reply we
        drafted and the step-by-step trace. We keep that the run happened, when,
        and what it cost.
      </p>
      <p className="hint">
        This covers every run by every team in your organisation, not only the
        automatic email ones.
      </p>

      <div className="field">
        <label htmlFor="retention-days">How long to keep run history</label>
        <select
          id="retention-days"
          value={days ?? ''}
          onChange={(e) => setDays(e.target.value === '' ? null : Number(e.target.value))}
        >
          {PERIODS.map((period) => (
            <option key={period.label} value={period.value ?? ''}>
              {period.label}
            </option>
          ))}
        </select>
      </div>

      {settings?.run_retention_days == null && (
        <p className="hint">
          Run history is kept forever at the moment. Nothing is removed until you
          choose a period.
        </p>
      )}
      {settings != null && settings.purgeable_now > 0 && (
        <p className="hint">
          The next cleanup will remove the content of {plural(settings.purgeable_now, 'past run')}.
        </p>
      )}
      {unsaved && <p className="hint">Not saved yet.</p>}
      {error && <p className="error">{error}</p>}
      {saved && !error && (
        <p className="muted">Saved. Nothing is removed until the next cleanup runs.</p>
      )}

      <button type="button" onClick={() => void save()} disabled={saving}>
        {saving ? 'Saving…' : 'Save'}
      </button>

      {settings?.last_swept_at && (
        <p className="muted">
          Last cleanup: {formatDateTime(settings.last_swept_at)}, removed{' '}
          {plural(settings.last_purged_count, 'run')}.
        </p>
      )}

      <div className="field">
        <h4>Take a copy first</h4>
        <p className="hint">
          Download everything a cleanup would remove, as one JSON file. Do this
          before you choose a period &mdash; once it is removed we cannot get it
          back.
        </p>
        <button type="button" onClick={() => void download()} disabled={exporting}>
          {exporting ? 'Preparing…' : 'Download export'}
        </button>
        {exportError && <p className="error">{exportError}</p>}
        {exported && !exportError && (
          <p className="muted">
            Downloaded {plural(exported.runs.length, 'run')}.
            {exported.truncated && ' This export hit our size limit, so the oldest runs are not in it.'}
          </p>
        )}
      </div>

      <div className="field">
        <h4>Remove history now</h4>
        <p className="hint">
          {days === null
            ? 'Choose a period above to use this.'
            : `This removes the content of every finished run older than ${plural(days, 'day')}, right now, without waiting for the next cleanup.`}
        </p>
        <label htmlFor="retention-confirm">Type {CONFIRM_WORD} to confirm</label>
        <input
          id="retention-confirm"
          type="text"
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          autoComplete="off"
        />
        <button
          type="button"
          onClick={() => void purge()}
          disabled={days === null || confirmation !== CONFIRM_WORD || purging}
        >
          {purging ? 'Deleting…' : 'Delete now'}
        </button>
        {purgeError && <p className="error">{purgeError}</p>}
        {purged !== null && !purgeError && (
          <p className="muted">Removed the content of {plural(purged, 'run')}.</p>
        )}
      </div>
    </section>
  )
}
