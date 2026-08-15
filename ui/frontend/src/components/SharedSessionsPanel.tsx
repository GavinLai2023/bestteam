import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import type { ShareLink, ShareMessage, ShareSessionSummary } from '../lib/types'

interface SharedSessionsPanelProps {
  workflowId: number
}

// Read-only audit view: every anonymous visitor session against every share
// link for one team, with a drill-in transcript. See docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md ("Frontend").
export default function SharedSessionsPanel({ workflowId }: SharedSessionsPanelProps) {
  const [links, setLinks] = useState<ShareLink[]>([])
  const [sessionsByLink, setSessionsByLink] = useState<Record<number, ShareSessionSummary[]>>({})
  const [transcript, setTranscript] = useState<{ linkId: number; sessionId: number; messages: ShareMessage[] } | null>(
    null,
  )
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // Reset stale state left over from the previous `workflowId` -- this
    // component isn't remounted on a team-switch (no `key` from
    // ActivityPage.tsx), so without this the old team's links/sessions (not
    // just an open transcript or error banner) would otherwise keep showing
    // while the new team's request is still in flight, silently mislabeled
    // as if they belonged to the newly-selected team (Codex review finding).
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset all panel state on workflow change
    setTranscript(null)
    setError(null)
    setLinks([])
    setSessionsByLink({})
    // Guards against an older request resolving after a newer one and
    // overwriting the panel with the wrong team's data (same pattern as
    // TracePage.tsx's own `ignore` flag for its fetch-on-selection effect).
    let ignore = false
    api
      .listShareLinks(workflowId)
      .then(async (fetchedLinks) => {
        const entries = await Promise.all(
          fetchedLinks.map(async (link) => [link.id, await api.listShareSessions(link.id)] as const),
        )
        if (ignore) return
        setLinks(fetchedLinks)
        setSessionsByLink(Object.fromEntries(entries))
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [workflowId])

  const openTranscript = async (linkId: number, sessionId: number) => {
    try {
      const messages = await api.getShareSessionMessages(linkId, sessionId)
      setTranscript({ linkId, sessionId, messages })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (transcript) {
    return (
      <div className="shared-sessions-transcript">
        <button type="button" onClick={() => setTranscript(null)}>
          Back
        </button>
        <ul>
          {transcript.messages.map((m, i) => (
            <li key={i} className={`share-chat-bubble ${m.role}`}>
              {m.content}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <div className="shared-sessions-panel">
      {error && <p className="banner banner-error">{error}</p>}
      {links.length === 0 && <p className="hint">No share links for this team yet.</p>}
      {links.map((link) => (
        <div key={link.id}>
          <h4>{link.active ? 'Active link' : 'Revoked link'}</h4>
          <ul>
            {(sessionsByLink[link.id] ?? []).map((session) => (
              <li key={session.id}>
                <span>Last active {formatDateTime(session.last_active_at)}</span>
                <span>{session.turns_today} turns today</span>
                <button type="button" onClick={() => openTranscript(link.id, session.id)}>
                  View transcript
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}
