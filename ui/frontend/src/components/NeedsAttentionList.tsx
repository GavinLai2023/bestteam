import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { AutomationResult } from '../lib/types'

interface NeedsAttentionListProps {
  onOpenRun?: (runId: string) => void
}

// Same cadence as EmailTriggerActivity's own poll -- see that component for
// the rationale (the backend's own trigger poller checks mail on its own
// schedule; refreshing faster just shows stale data sooner).
const REFRESH_INTERVAL_MS = 30_000

const PRIORITY_LABELS: Record<string, string> = {
  possible_emergency: 'Possible emergency',
  priority: 'Priority',
  routine: 'Routine',
  unknown: 'Unknown',
}

// Property Maintenance Inbox's "Needs attention" work list (spec section
// 6.3): the items from today's (and recent) automated triage that a human
// should look at. Deliberately no Approve/Assign/Close actions -- the spec
// is explicit that this must not grow into an implicit Case state machine;
// the customer reviews/sends in their own mailbox and continues in their
// existing PMS. `onOpenRun(runId)` lets the caller jump to that run's detail
// (e.g. by switching the Activity page to its Runs tab).
export default function NeedsAttentionList({ onOpenRun }: NeedsAttentionListProps) {
  const [results, setResults] = useState<AutomationResult[] | undefined>(undefined) // undefined = still loading
  const [error, setError] = useState(false)

  useEffect(() => {
    const load = () => {
      api
        .listAutomationResults({ needs_attention: true, limit: 20 })
        .then((data) => {
          setResults(data.results)
          setError(false)
        })
        .catch(() => setError(true))
    }
    load()
    const id = setInterval(load, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  if (error) {
    return <p className="banner banner-error">Couldn't load the needs-attention list. Refresh the page to try again.</p>
  }
  if (results === undefined) return null
  if (results.length === 0) return null // nothing needs attention -- no card at all

  return (
    <div className="wizard-card" style={{ marginBottom: '1rem' }}>
      <h3>Needs attention</h3>
      <ul className="needs-attention-list">
        {results.map((result) => {
          const payload = result.payload || {}
          const address = payload.extracted?.property_address || 'Address not identified'
          // A retention cleanup empties the payload and keeps the row -- its
          // status and source_key are what stop a retry re-drafting the same
          // message. Say the content was removed rather than render every
          // field as a blank or an "unknown", which reads as a failed triage.
          const purged = Object.keys(payload).length === 0
          return (
            <li key={result.id} className="needs-attention-item">
              <div className="needs-attention-item-header">
                {!purged && (
                  <span className={`status-badge priority-${payload.priority || 'unknown'}`}>
                    {PRIORITY_LABELS[payload.priority ?? ''] ?? payload.priority ?? 'Unknown'}
                  </span>
                )}
                <span className="hint">{formatDateTime(result.created_at)}</span>
              </div>
              {purged && <p className="hint">Content removed.</p>}
              {!purged && <p>{payload.summary || '(no summary)'}</p>}
              {!purged && <p className="hint">{address}</p>}
              {payload.human_reason && <p className="hint">Why: {payload.human_reason}</p>}
              <p className="hint">
                {/* Whether a draft was created is part of the removed payload,
                    so a purged item must not claim "No draft created". */}
                {!purged && (payload.action?.draft_created ? 'Draft created' : 'No draft created')}
                {!purged && ' · '}
                <button type="button" className="btn-link" onClick={() => onOpenRun?.(result.run_id)}>
                  View run
                </button>
              </p>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
