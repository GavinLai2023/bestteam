import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { DraftOutcomeCounts, EmailTrigger, FilteredMessage } from '../lib/types'

interface EmailTriggerActivityProps {
  onViewRuns?: () => void
}

const STATUS_META: Record<string, { badge: string; text: string }> = {
  active: { badge: 'Active', text: 'Watching for new email.' },
  off: { badge: 'Off', text: 'Automatic runs are turned off.' },
  disabled: { badge: 'Paused', text: 'Paused by the operator.' },
  paused_cap: { badge: 'Paused', text: "Today's run limit was reached — resumes tomorrow." },
  error: { badge: 'Problem', text: 'Problem checking the mailbox.' },
}

// How often to re-poll while mounted on the Activity page's Automations tab.
// The backend's own poller checks mail every BESTTEAM_TRIGGER_POLL_SECONDS
// (default 120s) -- refreshing faster than that would just show stale data
// sooner, so this trades a little staleness for not hammering the endpoint.
const REFRESH_INTERVAL_MS = 30_000

// Org-level automatic-runs status, shown on the Activity page's Automations
// tab. Always shows a status card once loaded -- including "off" -- so a
// deliberately-disabled trigger reads differently from never having
// configured one. Recent autonomous runs live on the Runs tab (filter:
// "Automatic only") instead of being duplicated here; `onViewRuns` jumps
// there pre-filtered.
//
// It also carries the mail the pre-LLM filter skipped. That is shown rather
// than dropped silently because a rule-based filter will have false positives,
// and the cost of one has to be "an admin clicks Release", not "the enquiry
// vanished and nobody knew".
export default function EmailTriggerActivity({ onViewRuns }: EmailTriggerActivityProps) {
  const [trigger, setTrigger] = useState<EmailTrigger | undefined>(undefined) // undefined = still loading
  const [statusFailed, setStatusFailed] = useState(false)
  const [filtered, setFiltered] = useState<FilteredMessage[]>([])
  // null = nothing to show (still loading, failed, or no draft tracked yet) --
  // in all three cases the line is simply absent rather than a row of zeros.
  const [outcomes, setOutcomes] = useState<DraftOutcomeCounts | null>(null)
  const [filteredFailed, setFilteredFailed] = useState(false)
  const [releaseError, setReleaseError] = useState<string | null>(null)
  // Ids released during this session. The list is re-polled below, and a
  // response already in flight when Release was clicked would otherwise put
  // the row straight back on screen.
  const releasedRef = useRef<Set<number>>(new Set())

  useEffect(() => {
    const load = () => {
      api.getEmailTrigger().then(setTrigger).catch(() => setStatusFailed(true))
      api
        .getDraftOutcomes()
        .then((counts) =>
          setOutcomes(counts.sent + counts.handled + counts.pending > 0 ? counts : null),
        )
        // Absent, not an error banner: the card's own status is the load-
        // bearing content; this line is a bonus metric.
        .catch(() => setOutcomes(null))
      api
        .listFilteredMessages()
        .then((data) => {
          setFiltered(data.filtered.filter((m) => !releasedRef.current.has(m.id)))
          setFilteredFailed(false)
        })
        // Reported, never swallowed: an empty section reads as "nothing was
        // skipped", which is the one thing a failed load must not claim.
        .catch(() => setFilteredFailed(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  const release = async (id: number) => {
    setReleaseError(null)
    try {
      await api.releaseFilteredMessage(id)
      // Drop the row here rather than refetching: it is already gone
      // server-side, and leaving it on screen until the admin reloads is the
      // exact defect the Phase 3b review found in RunDetail.
      releasedRef.current.add(id)
      setFiltered((rows) => rows.filter((row) => row.id !== id))
    } catch (e) {
      setReleaseError(e instanceof Error ? e.message : String(e))
    }
  }

  if (statusFailed) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <p className="banner banner-error">
          Couldn't load automatic-run status. Refresh the page to try again.
        </p>
      </div>
    )
  }

  if (trigger === undefined) return null // still loading -- avoid a flash

  if (!trigger.pipeline_name) {
    return (
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <h3>Automatic runs</h3>
        <p className="hint">
          No automatic runs configured yet. Connect a mailbox and turn one on from a team's
          Deploy page in the Team Builder.
        </p>
      </div>
    )
  }

  const meta = STATUS_META[trigger.status] ?? { badge: trigger.status, text: '' }

  return (
    <>
      <div className="wizard-card" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <div className="trigger-status-header">
          <h3>Automatic runs — "{trigger.pipeline_name}"</h3>
          <span className="status-badge">{meta.badge}</span>
        </div>
        <p className="subtitle">{meta.text}</p>
        <p className="hint">Triggers when new email arrives in the connected mailbox.</p>
        {outcomes && (
          <p className="hint">
            Drafts in the last {outcomes.window_days} days: {outcomes.sent} sent ·{' '}
            {outcomes.handled} handled · {outcomes.pending} awaiting action
          </p>
        )}
        {trigger.last_checked_at && (
          <p className="hint">
            Last checked: {formatDateTime(trigger.last_checked_at)}
          </p>
        )}
        {trigger.status === 'error' && trigger.last_error && (
          <p className="banner banner-error">{trigger.last_error}</p>
        )}
        <p className="hint">
          Turn this on, off, or point it at a different team from that team's Deploy page in the
          Team Builder.
        </p>
        {onViewRuns && (
          <button type="button" className="btn-link" onClick={onViewRuns}>
            View automatic runs
          </button>
        )}
      </div>

      <div className="wizard-card filtered-mail" style={{ background: '#f9fafb', marginBottom: '1rem' }}>
        <h3>Mail we skipped</h3>
        <p className="hint">
          Your filter rules skipped these before any AI model read them, so they cost
          nothing. A rule can be wrong &mdash; release a message and the next check will
          process it as normal.
        </p>
        {filteredFailed && (
          <p className="banner banner-error">
            Couldn't load the skipped mail. Refresh the page to try again.
          </p>
        )}
        {releaseError && <p className="banner banner-error">{releaseError}</p>}
        {filtered.length === 0 ? (
          !filteredFailed && <p className="hint">Nothing has been skipped.</p>
        ) : (
          <ul className="filtered-list">
            {filtered.map((message) => (
              <li key={message.id}>
                {/* `reason` is the sentence to read; `decision` is the rule that
                    fired, which is for debugging a rule, not for the admin. */}
                <p title={message.decision ?? undefined}>{message.reason}</p>
                <p className="muted">
                  Message {message.external_id}
                  {message.detected_at ? ` · ${formatDateTime(message.detected_at)}` : ''}
                </p>
                <button type="button" onClick={() => void release(message.id)}>
                  Release
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  )
}
