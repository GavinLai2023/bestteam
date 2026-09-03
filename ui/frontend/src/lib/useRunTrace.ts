import { useEffect, useRef, useState } from 'react'
import { WS_BASE, api } from './api'
import type { TraceEvent, UsageRecord } from './types'

interface RunTrace {
  events: TraceEvent[]
  usage: UsageRecord[]
  // When a retention cleanup removed this run's content, or null. Comes from
  // the same historical fetch as `events` -- a purged run has none left, and
  // without this the caller cannot tell that apart from a run that never
  // recorded any.
  contentPurgedAt: string | null
  // Operator-only: why a failed run really failed. The endpoint omits it for
  // anyone but a platform admin, so a customer's fetch always leaves this null
  // -- the gate is server-side, not a `hidden` in the component.
  internalError: string | null
  error: string | null
}

// A run's event timeline: a `running` run streams live over the same
// WebSocket MonitorPage uses; anything else reads its persisted trace via
// GET /api/runs/{id}/trace -- no live/historical merge, per the read
// endpoint's design (see docs/superpowers/specs). Shared by the
// customer-facing RunDetail and the admin TracePage's AdminRunDetail, which
// both need this same ticket-mint/WS/fetch dance.
//
// `usage` (per-agent token/cost) only ever comes from the historical fetch --
// the live WS stream carries events only, matching the same
// no-live/historical-merge design already used for automation results.
export function useRunTrace(runId: string, status: string): RunTrace {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [usage, setUsage] = useState<UsageRecord[]>([])
  const [contentPurgedAt, setContentPurgedAt] = useState<string | null>(null)
  const [internalError, setInternalError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  // Callers key their component by runId (see ActivityPage/TracePage) so
  // switching to a different run remounts it -- a fresh `events`/`usage`/
  // `error` state -- rather than needing to reset them here.
  useEffect(() => {
    if (status === 'running') {
      let cancelled = false
      ;(async () => {
        try {
          const { ticket } = await api.createWsTicket()
          if (cancelled) return
          const ws = new WebSocket(`${WS_BASE}/api/runs/${runId}/stream?ticket=${encodeURIComponent(ticket)}`)
          wsRef.current = ws
          ws.onmessage = (message: MessageEvent<string>) => {
            const event = JSON.parse(message.data) as TraceEvent
            setEvents((prev) => [...prev, event])
          }
          ws.onerror = () => setError("Couldn't stream this run.")
        } catch (e) {
          if (!cancelled) setError((e as Error).message)
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
        if (!ignore) {
          setEvents(data.events)
          setUsage(data.usage ?? [])
          setContentPurgedAt(data.content_purged_at ?? null)
          setInternalError(data.internal_error ?? null)
        }
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [runId, status])

  return { events, usage, contentPurgedAt, internalError, error }
}
