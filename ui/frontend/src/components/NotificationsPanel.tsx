import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { AppNotification } from '../lib/types'

interface NotificationsPanelProps {
  // Fires with the unread count whenever it's known, so a parent can show a
  // badge without fetching the list a second time.
  onUnreadChange?: (unread: number) => void
}

const SEVERITY_LABEL: Record<string, string> = {
  error: 'Problem',
  warning: 'Warning',
  info: 'Resolved',
}

// Only worth surfacing when it means the customer's own webhook isn't
// working. "skipped" means they never configured one, which is normal.
function deliveryNote(state: AppNotification['delivery_state']): string | null {
  if (state === 'failed') return "We couldn't reach your webhook for this alert."
  return null
}

// The org's alert history. Read-only by design: these are raised by the
// system, and letting someone delete the record of a fault they never fixed
// would defeat the point.
export default function NotificationsPanel({ onUnreadChange }: NotificationsPanelProps) {
  const [items, setItems] = useState<AppNotification[]>([])
  const [unread, setUnread] = useState(0)
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async (onlyUnread: boolean) => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.listNotifications(onlyUnread)
      setItems(data.notifications)
      setUnread(data.unread)
      onUnreadChange?.(data.unread)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount and on filter change
    void load(unreadOnly)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadOnly])

  const markRead = async (id: number) => {
    try {
      const result = await api.markNotificationRead(id)
      setItems((rows) => rows.map((r) => (r.id === id ? { ...r, read: true } : r)))
      setUnread(result.unread)
      onUnreadChange?.(result.unread)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  if (loading) return <p className="muted">Loading alerts&hellip;</p>

  return (
    <section className="notifications-panel">
      <header className="notifications-header">
        <h3>Alerts {unread > 0 && <span className="badge">{unread}</span>}</h3>
        <label>
          <input
            type="checkbox"
            checked={unreadOnly}
            onChange={(e) => setUnreadOnly(e.target.checked)}
          />{' '}
          Unread only
        </label>
      </header>

      {error && <p className="error">{error}</p>}

      {items.length === 0 ? (
        <p className="muted">
          {unreadOnly ? 'Nothing unread.' : 'No alerts. Automatic email runs are healthy.'}
        </p>
      ) : (
        <ul className="notification-list">
          {items.map((n) => (
            <li key={n.id} className={`notification ${n.severity}${n.read ? ' read' : ''}`}>
              <div className="notification-top">
                <span className={`severity ${n.severity}`}>
                  {SEVERITY_LABEL[n.severity] ?? n.severity}
                </span>
                <strong>{n.title}</strong>
                {n.created_at && <span className="muted">{formatDateTime(n.created_at)}</span>}
              </div>
              <p>{n.body}</p>
              {deliveryNote(n.delivery_state) && (
                <p className="muted">{deliveryNote(n.delivery_state)}</p>
              )}
              {!n.read && (
                <button type="button" onClick={() => void markRead(n.id)}>
                  Mark as read
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
