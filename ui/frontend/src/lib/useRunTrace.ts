import { useEffect, useRef, useState } from 'react'
import { WS_BASE, api } from './api'
import type { TraceEvent, UsageRecord } from './types'

interface RunTrace {
  events: TraceEvent[]
  usage: UsageRecord[]
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
        }
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [runId, status])

  return { events, usage, error }
}
