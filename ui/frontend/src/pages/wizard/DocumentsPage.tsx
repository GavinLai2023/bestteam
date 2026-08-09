import { useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { WizardOutletContext } from '../../lib/types'

const STAGE_LABELS: Record<string, string> = {
  uploading: 'Uploading your documents…',
  generating: 'Putting your team together…',
}

type Stage = null | 'uploading' | 'generating'

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
  const { session, setSession, loading, sessionId } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)

  const [label, setLabel] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState<Stage>(null)
  const [error, setError] = useState<string | null>(null)

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
    setBusy(true)

    if (useFiles) {
      setStage('uploading')
      try {
        await api.uploadOwnKnowledgeBaseFiles(slug, files)
      } catch (e) {
        setError((e as Error).message)
        setBusy(false)
        setStage(null)
        return
      }
    }

    setStage('generating')
    try {
      const updated = await api.submitSpecification(sessionId!, { model: pickDefaultModel(entries) })
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

      <div className="upload-section">
        <label className="btn btn-secondary" style={{ display: 'inline-block' }}>
          Choose files…
          <input
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={addFiles}
            disabled={busy}
            accept=".txt,.md,.csv,.json,.yaml,.yml,.log,.pdf,.xlsx,.xlsm,.docx"
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
    </div>
  )
}
