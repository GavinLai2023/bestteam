import { useCallback, useEffect, useState } from 'react'
import KnowledgeBaseSearch from './KnowledgeBaseSearch'
import { api } from '../lib/api'
import { useConfirm } from '../lib/useConfirm'
import type { OrgKnowledgeBase, OrgKnowledgeBaseDocument } from '../lib/types'
import './KnowledgeBasesPanel.css'

// Only while something is actually being indexed -- an idle "My teams" page
// must not poll this endpoint forever (same rule as the Activity page's run
// list, which polls only while a run is still running).
const POLL_INTERVAL_MS = 3000

function isProcessing(kb: OrgKnowledgeBase): boolean {
  return kb.latest_job?.status === 'queued' || kb.latest_job?.status === 'running'
}

// One customer-facing sentence per collection. The wording deliberately
// avoids ingestion-job vocabulary: the reader uploaded some documents and
// wants to know whether their team can search them yet.
function statusFor(kb: OrgKnowledgeBase): string {
  const job = kb.latest_job
  if (!job) return 'Not indexed yet'
  if (job.status === 'queued' || job.status === 'running') return 'Processing…'
  if (job.status === 'failed') {
    const reason = job.errors[0]?.error ?? 'the documents could not be processed'
    // A failed *re*-upload leaves the previous generation live, so the row
    // has to say so -- otherwise the reader deletes a collection their team
    // is still successfully using.
    const stillLive = kb.servable ? ' — an earlier version is still in use.' : ''
    return `Failed: ${reason}${stillLive}`
  }
  const n = job.documents_succeeded
  return `Ready · ${n} document${n === 1 ? '' : 's'}`
}

// Why Delete is refused, in the reader's own terms, or null when it's
// allowed. The backend refuses both cases with a 409 regardless; this only
// saves the reader a pointless click.
function deleteBlockedReason(kb: OrgKnowledgeBase): string | null {
  if (isProcessing(kb)) return 'This upload is still processing. Wait for it to finish, then delete it.'
  if (kb.used_by.length > 0) {
    return `In use by ${kb.used_by.join(', ')}. Update or remove those teams first.`
  }
  return null
}

// Why removing one document is refused, in the reader's own terms, or null
// when it's allowed. The backend refuses both with a 409 regardless.
function removeBlockedReason(kb: OrgKnowledgeBase): string | null {
  if (isProcessing(kb)) return 'This upload is still processing. Wait for it to finish, then remove a document.'
  if (kb.documents.length <= 1) {
    return 'This is the only document. To remove it, delete the whole collection.'
  }
  return null
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Why "Try a search" is refused, in the reader's own terms, or null when it's
// allowed. Mirrors the endpoint's own 409 cases, which it cannot resolve for
// them: nothing has finished indexing yet, or something still is. A legacy
// collection served from disk reports `servable`, so its own refusal only
// surfaces once the search is actually run -- the list has no way to tell.
function searchBlockedReason(kb: OrgKnowledgeBase): string | null {
  if (isProcessing(kb)) return 'This upload is still processing. Wait for it to finish, then try a search.'
  if (!kb.servable) return 'There is nothing to search yet — no upload has finished successfully.'
  return null
}

// The org's own document collections, listed under "My teams" so a customer
// can see what they uploaded, whether it worked, which teams depend on it,
// and remove one -- none of which previously existed outside the admin
// Advanced page.
export default function KnowledgeBasesPanel() {
  const [confirmNode, confirm] = useConfirm()
  const [items, setItems] = useState<OrgKnowledgeBase[]>([])
  const [error, setError] = useState<string | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [openSkipped, setOpenSkipped] = useState<string | null>(null)
  const [openSearch, setOpenSearch] = useState<string | null>(null)
  const [openDocuments, setOpenDocuments] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setItems(await api.listOwnKnowledgeBases())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const anyProcessing = items.some(isProcessing)
  useEffect(() => {
    if (!anyProcessing) return undefined
    const id = setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [anyProcessing, refresh])

  const handleDelete = async (kb: OrgKnowledgeBase) => {
    const ok = await confirm({
      title: `Delete "${kb.name}"?`,
      body: 'Its documents are removed and teams can no longer search them.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    try {
      await api.deleteOwnKnowledgeBase(kb.name)
      setItems((prev) => prev.filter((i) => i.name !== kb.name))
    } catch (e) {
      // Shown on the row it belongs to: the 409 names the teams still using
      // this particular collection, which is meaningless at page level.
      setRowErrors((prev) => ({ ...prev, [kb.name]: (e as Error).message }))
    }
  }

  const handleRemoveDocument = async (kb: OrgKnowledgeBase, doc: OrgKnowledgeBaseDocument) => {
    // Removing a document from a collection a live team searches is allowed
    // (so is adding one), but the reader should know whose answers change.
    const usedBy = kb.used_by.length > 0 ? ` Teams using "${kb.name}": ${kb.used_by.join(', ')}.` : ''
    const ok = await confirm({
      title: `Remove "${doc.filename}"?`,
      body: `It is taken out of "${kb.name}" and teams can no longer search it.${usedBy}`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    try {
      await api.removeOwnKnowledgeBaseDocument(kb.name, doc.filename)
      setRowErrors((prev) => {
        const next = { ...prev }
        delete next[kb.name]
        return next
      })
      // The row now shows "Processing…" and the poll above picks up the
      // finished generation, documents list included.
      await refresh()
    } catch (e) {
      setRowErrors((prev) => ({ ...prev, [kb.name]: (e as Error).message }))
    }
  }

  if (items.length === 0 && !error) return null

  return (
    <section className="knowledge-bases-panel">
      <div className="session-status-header">
        <h2>My documents</h2>
        <button type="button" className="btn btn-secondary" onClick={() => void refresh()}>
          Refresh
        </button>
      </div>
      <p className="subtitle">
        Files you've uploaded. Any team you build automatically gets to search these -- there's nothing to attach by
        hand.
      </p>

      {error && <p className="banner banner-error">{error}</p>}

      <ul className="session-list">
        {items.map((kb) => {
          const job = kb.latest_job
          // `errors` is a sample -- `job_status_payload` caps it at 10 -- so the
          // count comes from `documents_failed` and the list stays what we can name.
          const skipped = job?.status === 'completed' ? job.errors : []
          const skippedCount = job?.status === 'completed' ? job.documents_failed : 0
          const blocked = deleteBlockedReason(kb)
          const searchBlocked = searchBlockedReason(kb)
          const removeBlocked = removeBlockedReason(kb)
          const docCount = kb.documents.length
          return (
            <li key={kb.name} className="session-item kb-card">
              <h3>{kb.name}</h3>
              <p className="hint">{statusFor(kb)}</p>
              {kb.used_by.length > 0 && <p className="hint">Used by {kb.used_by.join(', ')}</p>}
              {skippedCount > 0 && (
                <>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => setOpenSkipped((n) => (n === kb.name ? null : kb.name))}
                  >
                    {skippedCount} file{skippedCount === 1 ? '' : 's'} skipped
                  </button>
                  {openSkipped === kb.name && (
                    <ul>
                      {skipped.map((e, i) => (
                        <li key={i} className="hint">
                          {e.filename ?? 'This upload'}: {e.error}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
              {docCount > 0 && (
                <>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => setOpenDocuments((n) => (n === kb.name ? null : kb.name))}
                  >
                    {openDocuments === kb.name ? 'Hide documents' : `Show ${docCount} document${docCount === 1 ? '' : 's'}`}
                  </button>
                  {openDocuments === kb.name && (
                    <ul className="kb-documents">
                      {kb.documents.map((doc) => (
                        <li key={doc.filename} className="kb-document">
                          <span className="kb-document-name">{doc.filename}</span>
                          <span className="hint">
                            {formatSize(doc.size_bytes)}
                            {doc.status === 'failed' ? " · couldn't be read" : ''}
                          </span>
                          <button
                            type="button"
                            className="btn-link kb-document-remove"
                            disabled={removeBlocked !== null}
                            title={removeBlocked ?? `Remove ${doc.filename}`}
                            aria-label={`Remove ${doc.filename}`}
                            onClick={() => void handleRemoveDocument(kb, doc)}
                          >
                            Remove
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
              {rowErrors[kb.name] && <p className="banner banner-error">{rowErrors[kb.name]}</p>}
              <button
                type="button"
                className="btn btn-secondary"
                disabled={searchBlocked !== null}
                title={searchBlocked ?? 'Try a search'}
                onClick={() => setOpenSearch((n) => (n === kb.name ? null : kb.name))}
              >
                Try a search
              </button>
              <button
                type="button"
                className="btn btn-danger-outline"
                disabled={blocked !== null}
                title={blocked ?? 'Delete'}
                onClick={() => void handleDelete(kb)}
              >
                Delete
              </button>
              {openSearch === kb.name && <KnowledgeBaseSearch name={kb.name} />}
            </li>
          )
        })}
      </ul>
      {confirmNode}
    </section>
  )
}
