import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ShareLink } from '../lib/types'

interface ShareLinksPanelProps {
  workflowId: number
}

function shareUrlFor(token: string): string {
  return `${window.location.origin}/share/${token}`
}

// Lets the org's one user generate/revoke anonymous, continuous-chat links
// for a deployed team (see docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md). Rendered inline on
// each deployed team's card in "My teams" (SessionsPage.tsx). Collapsed by
// default -- SessionsPage can list many teams, and this keeps the page from
// firing a share-links fetch per card on every load; the list only loads
// once the user opts in by clicking "Share".
export default function ShareLinksPanel({ workflowId }: ShareLinksPanelProps) {
  const [links, setLinks] = useState<ShareLink[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<number | null>(null)

  const refresh = () => {
    api
      .listShareLinks(workflowId)
      .then(setLinks)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    if (open) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleCreate = async () => {
    try {
      await api.createShareLink(workflowId, {})
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleRevoke = async (linkId: number) => {
    try {
      await api.patchShareLink(linkId, { active: false })
      refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleCopy = async (link: ShareLink) => {
    await navigator.clipboard.writeText(shareUrlFor(link.token))
    setCopiedId(link.id)
    setTimeout(() => setCopiedId(null), 2000)
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Share
      </button>
    )
  }

  return (
    <div className="share-links-panel" onClick={(e) => e.stopPropagation()}>
      {error && <p className="banner banner-error">{error}</p>}
      <button type="button" onClick={handleCreate}>
        Generate a new link
      </button>
      <ul>
        {links.map((link) => (
          <li key={link.id}>
            <span>{link.active ? 'Active' : 'Revoked'}</span>
            {link.active && (
              <>
                <button type="button" onClick={() => handleCopy(link)}>
                  {copiedId === link.id ? 'Copied!' : 'Copy link'}
                </button>
                <button type="button" onClick={() => handleRevoke(link.id)}>
                  Revoke
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      <button type="button" onClick={() => setOpen(false)}>
        Close
      </button>
    </div>
  )
}
