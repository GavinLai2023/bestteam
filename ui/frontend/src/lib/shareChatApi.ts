import { API_BASE, WS_BASE } from './api'
import type { ShareMessage, ShareTeamInfo } from './types'

// The public, anonymous counterpart to lib/api.ts's authenticated `request`.
// No bearer token: the visitor's identity is a signed session cookie the
// backend sets via Set-Cookie on the first message (share_chat.py), so every
// call here must send credentials -- unlike api.ts's `request`, which never
// needs cookies at all.

interface ApiError extends Error {
  status?: number
}

async function shareRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers as Record<string, string> | undefined) },
    ...options,
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      // no JSON body
    }
    const error: ApiError = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    error.status = res.status
    throw error
  }
  return res.json()
}

export const shareChatApi = {
  sendMessage: (token: string, content: string) =>
    shareRequest<{ run_id: string; turn_number: number }>(`/api/share/${encodeURIComponent(token)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  getMessages: (token: string) =>
    shareRequest<{ messages: ShareMessage[] }>(`/api/share/${encodeURIComponent(token)}/messages`),
  getTeam: (token: string) => shareRequest<ShareTeamInfo>(`/api/share/${encodeURIComponent(token)}/team`),
  cancelRun: (token: string, runId: string) =>
    shareRequest<{ cancelled: boolean }>(
      `/api/share/${encodeURIComponent(token)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' },
    ),
  streamUrl: (token: string, runId: string) =>
    `${WS_BASE}/api/share/${encodeURIComponent(token)}/stream/${encodeURIComponent(runId)}`,
}
