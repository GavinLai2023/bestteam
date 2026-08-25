import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { FeedbackItem } from '../lib/types'
import '../components/WizardLayout.css'
import './AdvancedPage.css'
import './FeedbackPage.css'

const STATUSES = ['new', 'acknowledged', 'resolved', 'dismissed']
const KINDS = ['defect', 'suggestion']

// Admin-only triage of user/visitor feedback (defects and suggestions).
// English literals for the content, like MemoryPage: admin pages are not
// part of the bilingual customer surface. Bodies are attacker-authored on
// the share side, so they render as plain text only -- never markdown.
export default function FeedbackPage() {
  const [items, setItems] = useState<FeedbackItem[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [kindFilter, setKindFilter] = useState('')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [statusDraft, setStatusDraft] = useState('')
  const [noteDraft, setNoteDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (opts: { status?: string; kind?: string } = {}) => {
    setLoading(true)
    setError(null)
    api
      .adminFeedback({ status: opts.status ?? statusFilter, kind: opts.kind ?? kindFilter })
      .then((data) => setItems(data.feedback))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount only
  }, [])

  const filterByStatus = (status: string) => {
    setStatusFilter(status)
    load({ status })
  }

  const filterByKind = (kind: string) => {
    setKindFilter(kind)
    load({ kind })
  }

  const toggleRow = (item: FeedbackItem) => {
    if (expandedId === item.id) {
      setExpandedId(null)
      return
    }
    setExpandedId(item.id)
    setStatusDraft(item.status)
    setNoteDraft(item.admin_note ?? '')
  }

  const save = async (item: FeedbackItem) => {
    setSaving(true)
    setError(null)
    try {
      await api.patchFeedback(item.id, { status: statusDraft, admin_note: noteDraft })
      load()
      setExpandedId(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="advanced-page">
      <h1>Feedback</h1>
      <p className="hint">
        Defect reports and suggestions from logged-in users and share-link visitors. Bodies are
        shown verbatim as plain text.
      </p>

      <div className="feedback-filters">
        <label>
          Status
          <select value={statusFilter} onChange={(e) => filterByStatus(e.target.value)}>
            <option value="">All</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </label>
        <label>
          Kind
          <select value={kindFilter} onChange={(e) => filterByKind(e.target.value)}>
            <option value="">All</option>
            {KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <div className="banner banner-error">{error}</div>}
      {loading && <p>Loading…</p>}
      {!loading && items.length === 0 && !error && <p>No feedback yet.</p>}

      <ul className="feedback-list">
        {items.map((item) => (
          <li key={item.id} className="feedback-item">
            <button type="button" className="feedback-row" onClick={() => toggleRow(item)}>
              <span className={`feedback-chip feedback-chip-${item.kind}`}>{item.kind}</span>
              <span className={`feedback-chip feedback-chip-${item.status}`}>{item.status}</span>
              <span className="feedback-meta">
                {item.created_at ? formatDateTime(item.created_at) : '—'}
                {' · '}
                {item.org_name ?? '—'}
                {' · '}
                {item.source === 'user' ? (item.username ?? 'user') : 'visitor'}
              </span>
              <span className="feedback-excerpt">{item.body}</span>
            </button>
            {expandedId === item.id && (
              <div className="feedback-detail">
                {/* Plain text on purpose: visitor-authored. */}
                <pre className="feedback-body">{item.body}</pre>
                {item.context && (
                  <dl className="feedback-context">
                    {Object.entries(item.context).map(([key, value]) => (
                      <div key={key}>
                        <dt>{key}</dt>
                        <dd>{String(value)}</dd>
                      </div>
                    ))}
                  </dl>
                )}
                <div className="feedback-triage">
                  <label>
                    Set status
                    <select
                      value={statusDraft}
                      onChange={(e) => setStatusDraft(e.target.value)}
                      disabled={saving}
                    >
                      {STATUSES.map((status) => (
                        <option key={status} value={status}>
                          {status}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Note
                    <textarea
                      rows={2}
                      value={noteDraft}
                      onChange={(e) => setNoteDraft(e.target.value)}
                      disabled={saving}
                    />
                  </label>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void save(item)}
                    disabled={saving}
                  >
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
