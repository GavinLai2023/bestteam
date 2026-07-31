import { useEffect, useRef, useState } from 'react'
import { WS_BASE, api } from '../lib/api'
import { EVENT_LABELS, RESULT_LABELS, TERMINAL_TYPES, renderEventData } from '../lib/traceEvents'
import '../pages/MonitorPage.css' // reuses .event/.event-*/.result styling

// A run's event timeline, for the Activity page's Runs tab. A `running` run
// streams live over the same WebSocket MonitorPage uses; anything else reads
// its persisted trace via GET /api/runs/{id}/trace -- no live/historical
// merge, per the read endpoint's design (see docs/superpowers/specs).
export default function RunDetail({ runId, status }) {
  const [events, setEvents] = useState([])
  const [error, setError] = useState(null)
  const wsRef = useRef(null)

  // Callers key this component by runId (see ActivityPage) so switching to a
  // different run remounts it -- a fresh `events`/`error` state -- rather
  // than needing to reset them here.
  useEffect(() => {
    if (status === 'running') {
      let cancelled = false
      ;(async () => {
        try {
          const { ticket } = await api.createWsTicket()
          if (cancelled) return
          const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
          wsRef.current = ws
          ws.onmessage = (message) => {
            const event = JSON.parse(message.data)
            setEvents((prev) => [...prev, event])
          }
          ws.onerror = () => setError("Couldn't stream this run.")
        } catch (e) {
          if (!cancelled) setError(e.message)
        }
      })()
      return () => {
        cancelled = true
        wsRef.current?.close()
      }
    }

    let ignore = false
    api
      .getRunTrace(runId)
      .then((data) => {
        if (!ignore) setEvents(data.events)
      })
      .catch((e) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [runId, status])

  const finalEvent = events.find((e) => TERMINAL_TYPES.includes(e.type))

  return (
    <div className="run-detail">
      {error && <p className="banner banner-error">{error}</p>}
      {events.length === 0 && !error ? (
        <p className="hint">{status === 'running' ? 'Waiting for events…' : 'No trace recorded for this run.'}</p>
      ) : (
        <ul className="run-detail-events">
          {events.map((event, i) => (
            <li key={i} className={`event event-${event.type}`}>
              <span className="event-type">{EVENT_LABELS[event.type] ?? event.type}</span>
              {event.agent && <span className="event-agent">{event.agent}</span>}
              <p className="event-data">{renderEventData(event)}</p>
            </li>
          ))}
        </ul>
      )}
      {finalEvent && (
        <section className={`result result-${finalEvent.type}`}>
          <h3>{RESULT_LABELS[finalEvent.type]}</h3>
          <p>{finalEvent.data}</p>
        </section>
      )}
    </div>
  )
}
