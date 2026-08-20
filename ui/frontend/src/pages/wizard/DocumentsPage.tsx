import { useEffect, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'
import { useConfirm } from '../../lib/useConfirm'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { WizardOutletContext } from '../../lib/types'

const STAGE_LABELS: Record<string, string> = {
  uploading: 'Uploading your documents…',
  ingesting: 'Processing your documents…',
  generating: 'Putting your team together…',
}

type Stage = null | 'uploading' | 'ingesting' | 'generating'

const POLL_INTERVAL_MS = 500
// ~1 minute of polling. Nothing reconciles a queued/running ingestion job left
// behind by a backend restart, so an uncapped loop leaves the customer staring
// at "Processing your documents…" with no escape but a page reload.
const POLL_MAX_ATTEMPTS = 120

// Uploading now just queues ingestion; poll the job until it's done before
// moving on to spec generation. Returns null if the job is still unresolved
// when the cap is reached -- the upload itself succeeded and keeps processing
// server-side, so that's neither success nor failure.
async function pollIngestionJob(
  slug: string,
  jobId: number,
): Promise<import('../../lib/types').IngestionJobStatus | null> {
  for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
    const job = await api.orgKnowledgeBaseUploadJob(slug, jobId)
    if (job.status === 'completed' || job.status === 'failed') return job
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
  }
  return null
}

// Turns a free-text label into the identifier the backend stores the
// knowledge base under and an agent later references by name -- letters,
// numbers, and underscores only (matches the server's own charset, which
// also allows hyphens, but underscores read better from a "type a label" box).
function slugify(label: string): string {
  return label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64)
}

export default function DocumentsPage() {
  const [confirmNode, confirm] = useConfirm()
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)

  const [label, setLabel] = useState('')
  // Optional. It becomes the agent tool's own description, so it is what
  // tells the AI team which collection to search for a given question.
  const [description, setDescription] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState<Stage>(null)
  const [error, setError] = useState<string | null>(null)
  // "Still processing" -- neither an error nor a success, so it gets its own
  // informational banner rather than reusing the error one.
  const [notice, setNotice] = useState<string | null>(null)
  // Whether the operator has configured a default embedding model for the
  // "smart search" upgrade -- unset means the toggle below never renders.
  const [smartSearchAvailable, setSmartSearchAvailable] = useState(false)
  // Defaults to Enhanced once available -- it's the better experience and
  // the toggle only ever appears when the operator opted the deployment in.
  const [smartSearch, setSmartSearch] = useState(true)

  useEffect(() => {
    api
      .orgKnowledgeBaseCapabilities()
      .then((caps) => setSmartSearchAvailable(caps.smart_search_available))
      .catch(() => setSmartSearchAvailable(false))
  }, [])

  if (loading) return <p className="hint">Loading…</p>
  if (!session) return null

  const addFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? [])
    e.target.value = '' // reset so the same file can be re-selected
    if (picked.length === 0) return
    setFiles((prev) => [...prev, ...picked])
  }

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index))
  }

  const proceed = async (skip: boolean) => {
    if (busy || catalogLoading || catalogUnavailable) return
    const useFiles = !skip && files.length > 0
    const slug = slugify(label)
    if (useFiles && !slug) {
      setError('Give your documents a short name first (e.g. "Product policies").')
      return
    }

    setError(null)
    setNotice(null)
    setBusy(true)

    const smartSearchEnabled = smartSearchAvailable && smartSearch
    const kbDescription = description.trim() || undefined

    if (useFiles) {
      setStage('uploading')
      let uploadResult: { job_id: number }
      try {
        uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, false, smartSearchEnabled, kbDescription)
      } catch (e) {
        const err = e as Error & { status?: number }
        if (err.status === 409) {
          // The 409 detail says what the existing collection is like today;
          // this says what it would become, so both halves of the change are
          // in the one dialog the customer has to answer.
          const ok = await confirm({
            title: 'Replace these documents?',
            body: `${err.message} They will be re-indexed with ${
              smartSearchEnabled ? 'Enhanced' : 'Standard'
            } search.`,
            confirmLabel: 'Replace',
            destructive: true,
          })
          if (!ok) {
            setBusy(false)
            setStage(null)
            return
          }
          try {
            uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, true, smartSearchEnabled, kbDescription)
          } catch (e2) {
            setError((e2 as Error).message)
            setBusy(false)
            setStage(null)
            return
          }
        } else {
          setError(err.message)
          setBusy(false)
          setStage(null)
          return
        }
      }

      setStage('ingesting')
      try {
        const job = await pollIngestionJob(slug, uploadResult.job_id)
        if (job === null) {
          // Distinct from success and from failure: the documents are still
          // being processed, so don't generate a spec against a knowledge
          // base that isn't queryable yet -- and don't claim it failed either.
          setNotice(
            'Your documents are still being processed — this is taking longer than expected. ' +
              'They’re safely uploaded; come back in a moment and continue from here.',
          )
          setBusy(false)
          setStage(null)
          return
        }
        if (job.status === 'failed') {
          const detail = job.errors[0]?.error
          setError(detail ? `Processing failed: ${detail}` : 'Processing your documents failed.')
          setBusy(false)
          setStage(null)
          return
        }
      } catch (e) {
        setError((e as Error).message)
        setBusy(false)
        setStage(null)
        return
      }
    }

    setStage('generating')
    try {
      // Tell the architect exactly which knowledge base was just uploaded --
      // without this it only sees the org's whole KB catalog, which can
      // leave a new upload unattached (or the wrong one picked) if the org
      // already has other collections (Codex review finding).
      const kbHint = useFiles
        ? `The customer just uploaded documents to a knowledge base named "${slug}". Make sure at least one agent's tools includes it.`
        : ''
      // Revisiting this page after a specification already exists (the
      // Confirm page's "add or update documents" link) must refine that
      // design, not regenerate one from scratch -- regenerating silently
      // discards any solution feedback already applied (Codex review finding).
      const updated = session.specification_json
        ? await api.submitSolution(sessionId!, { feedback: kbHint, model: pickDefaultModel(entries) })
        : await api.submitSpecification(sessionId!, { model: pickDefaultModel(entries), feedback: kbHint || undefined })
      setSession(updated)
      navigate(`/wizard/${sessionId}/preview`)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
      setStage(null)
    }
  }

  return (
    <div className="wizard-card">
      <h2>Add your documents</h2>
      <p className="subtitle">
        If your AI team should be able to answer questions from your own files — policies, FAQs, manuals — upload
        them here. Optional: you can always skip this and add documents later.
      </p>

      {catalogUnavailable && (
        <div className="banner banner-error">
          {catalogFailed
            ? "Couldn't load the available AI models. Check your connection and try again."
            : 'No AI models are available yet. Contact your administrator, or try again.'}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retryCatalog}>
              Try again
            </button>
          </div>
        </div>
      )}

      {notice && <div className="banner banner-info">{notice}</div>}

      {error && (
        <div className="banner banner-error">
          {error}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={() => proceed(false)} disabled={busy}>
              Try again
            </button>
          </div>
        </div>
      )}

      <div className="field">
        <label htmlFor="doc-label">
          What should we call these documents? <span className="hint">(required if you're uploading)</span>
        </label>
        <input
          id="doc-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. Product policies"
          disabled={busy}
        />
      </div>

      <div className="field">
        <label htmlFor="doc-description">
          What's in these documents? (one sentence) <span className="hint">(optional)</span>
        </label>
        <input
          id="doc-description"
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="e.g. Refund, delivery and warranty policies for our online shop"
          maxLength={500}
          disabled={busy}
        />
        <p className="hint">This helps your AI team know when to look here for an answer.</p>
      </div>

      {smartSearchAvailable && (
        <div className="field">
          <label>Search quality</label>
          <div className="wizard-actions" style={{ justifyContent: 'flex-start', gap: 8, marginBottom: 4 }}>
            <button
              type="button"
              className={`btn ${smartSearch ? 'btn-secondary' : 'btn-primary'}`}
              onClick={() => setSmartSearch(false)}
              disabled={busy}
            >
              Standard
            </button>
            <button
              type="button"
              className={`btn ${smartSearch ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => setSmartSearch(true)}
              disabled={busy}
            >
              Enhanced
            </button>
          </div>
          <p className="hint">
            Enhanced finds more relevant answers in your documents. Takes a little longer to index.
          </p>
        </div>
      )}

      <div className="upload-section">
        <label className="btn btn-secondary" style={{ display: 'inline-block' }}>
          Choose files…
          <input
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={addFiles}
            disabled={busy}
            accept=".txt,.md,.csv,.json,.yaml,.yml,.log,.pdf,.xlsx,.xlsm,.docx,.xml"
          />
        </label>
      </div>

      {files.length > 0 && (
        <ul className="tag-list" style={{ marginBottom: 16 }}>
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`}>
              {f.name}{' '}
              <button
                type="button"
                onClick={() => removeFile(i)}
                disabled={busy}
                aria-label={`Remove ${f.name}`}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', padding: 0 }}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="wizard-actions">
        <button className="btn btn-secondary" onClick={() => proceed(true)} disabled={busy || catalogLoading || catalogUnavailable}>
          Skip for now
        </button>
        <button
          className="btn btn-primary"
          onClick={() => proceed(false)}
          disabled={busy || catalogLoading || catalogUnavailable || (files.length > 0 && !slugify(label))}
        >
          {busy ? STAGE_LABELS[stage ?? ''] ?? 'Working…' : 'Continue'}
        </button>
      </div>
      {confirmNode}
    </div>
  )
}
