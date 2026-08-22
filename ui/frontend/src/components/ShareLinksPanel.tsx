import { FormEvent, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { endOfLocalDay, formatDateTime } from '../lib/dateFormat'
import type { ShareLink } from '../lib/types'

interface ShareLinksPanelProps {
  pipelineId: number
}

const DEFAULT_DAILY_CAP = 30 // mirrors share_links_api.ShareLinkCreate's default and 1..1000 range

function shareUrlFor(token: string): string {
  return `${window.location.origin}/share/${token}`
}

// Lets the org's one user generate/revoke anonymous, continuous-chat links
// for a deployed team (see docs/superpowers/specs/
// 2026-08-14-team-sharing-continuous-chat-design.md). Rendered inline on
// each deployed team's card in "My teams" (SessionsPage.tsx). Collapsed by
// default -- SessionsPage can list many teams, and this keeps the page from
// firing a share-links fetch per card on every load; the list only loads
// once the user opts in by clicking "Share". A link's daily cap and expiry
// are set at creation only: to change them, revoke and generate another.
export default function ShareLinksPanel({ pipelineId }: ShareLinksPanelProps) {
  const { t } = useTranslation()
  const [links, setLinks] = useState<ShareLink[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<number | null>(null)
  const [dailyCap, setDailyCap] = useState(String(DEFAULT_DAILY_CAP))
  const [expiresOn, setExpiresOn] = useState('')

  const refresh = () => {
    api
      .listShareLinks(pipelineId)
      .then(setLinks)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    if (open) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    // Validate here rather than rely on the browser: the API field is an
    // integer in 1..1000, and a 422 from it is not a sentence a customer can
    // act on (Codex review).
    const cap = Number(dailyCap)
    if (!Number.isInteger(cap) || cap < 1 || cap > 1000) {
      setError(t('shareLinks.invalidCap'))
      return
    }
    const payload: { daily_cap: number; expires_at?: string } = { daily_cap: cap }
    if (expiresOn) payload.expires_at = endOfLocalDay(expiresOn).toISOString()
    setError(null)
    try {
      await api.createShareLink(pipelineId, payload)
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
    // `navigator.clipboard` rejects (or is undefined) in a non-secure
    // context -- any HTTP origin that isn't localhost -- so this can't be
    // left unguarded.
    try {
      await navigator.clipboard.writeText(shareUrlFor(link.token))
      setCopiedId(link.id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      setError(t('shareLinks.copyFailed'))
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
        {t('shareLinks.toggle')}
      </button>
    )
  }

  return (
    <div className="share-links-panel" onClick={(e) => e.stopPropagation()}>
      {error && <p className="banner banner-error">{error}</p>}
      <form className="share-links-form" onSubmit={(e) => void handleCreate(e)}>
        <label>
          {t('shareLinks.messagesPerDay')}
          <input
            type="number"
            min={1}
            max={1000}
            step={1}
            value={dailyCap}
            onChange={(e) => setDailyCap(e.target.value)}
          />
        </label>
        <label>
          {t('shareLinks.expiresOn')}
          <input type="date" value={expiresOn} onChange={(e) => setExpiresOn(e.target.value)} />
        </label>
        <button type="submit" className="btn btn-primary">
          {t('shareLinks.generate')}
        </button>
      </form>
      <ul>
        {links.map((link) => (
          <li key={link.id}>
            <span>{link.active ? t('shareLinks.active') : t('shareLinks.revoked')}</span>
            <span>{t('shareLinks.perDay', { n: link.daily_cap })}</span>
            <span>
              {link.expires_at
                ? t('shareLinks.expires', { when: formatDateTime(link.expires_at) })
                : t('shareLinks.noExpiry')}
            </span>
            {link.active && (
              <>
                <button type="button" className="btn btn-secondary" onClick={() => handleCopy(link)}>
                  {copiedId === link.id ? t('shareLinks.copied') : t('shareLinks.copyLink')}
                </button>
                <button type="button" className="btn btn-danger-outline" onClick={() => handleRevoke(link.id)}>
                  {t('shareLinks.revoke')}
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      <button type="button" className="btn-link" onClick={() => setOpen(false)}>
        {t('shareLinks.close')}
      </button>
    </div>
  )
}
