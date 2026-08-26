import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'
import { useConfirm } from '../../lib/useConfirm'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { OrgKnowledgeBase, WizardOutletContext } from '../../lib/types'

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
  const { t } = useTranslation()
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

  // The org's own collections, fetched once so this page can show what's
  // already in a collection instead of only finding out at 409-time.
  const [existingKbs, setExistingKbs] = useState<OrgKnowledgeBase[]>([])
  // The filename currently being removed from an already-ingested
  // collection, so only one removal request is in flight at a time.
  const [removingFile, setRemovingFile] = useState<string | null>(null)
  // A failed removal gets its own banner beside the file list rather than the
  // page-wide `error` one, whose "Try Again" runs `proceed()` -- spec
  // generation, a billable model call, and not what failed (Codex review).
  const [removalError, setRemovalError] = useState<string | null>(null)
  // Set once an upload to a collection that already existed before this visit
  // finishes ingesting -- pauses here so the customer sees the merged
  // old+new file list before the page moves on to spec generation.
  const [reviewingSlug, setReviewingSlug] = useState<string | null>(null)

  useEffect(() => {
    api
      .orgKnowledgeBaseCapabilities()
      .then((caps) => setSmartSearchAvailable(caps.smart_search_available))
      .catch(() => setSmartSearchAvailable(false))
  }, [])

  useEffect(() => {
    api
      .listOwnKnowledgeBases()
      .then(setExistingKbs)
      .catch(() => setExistingKbs([]))
  }, [])

  // Which of the org's existing collections this team's agents already
  // search -- ground truth from each agent's own tool list, never a guess:
  // a collection name can never collide with a built-in/skill tool name
  // (enforced at deploy time), so this intersection is exact.
  const usedKbNames = useMemo(() => {
    if (!session?.specification_json) return []
    const referenced = new Set(session.specification_json.agents.flatMap((a) => a.tools ?? []))
    return existingKbs.map((kb) => kb.name).filter((name) => referenced.has(name))
  }, [session, existingKbs])

  // The name to act on: whatever the customer typed or picked, falling back
  // to the one collection detection resolved to -- derived at render rather
  // than copied into `label` via an effect, so there's no separate "did we
  // already prefill it" state to keep in sync.
  const effectiveLabel = label || (usedKbNames.length === 1 ? usedKbNames[0] : '')

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  const addFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? [])
    e.target.value = '' // reset so the same file can be re-selected
    if (picked.length === 0) return
    setFiles((prev) => [...prev, ...picked])
  }

  const stageLabel = () => {
    if (stage === 'uploading') return t('wizard.documents.uploading')
    if (stage === 'ingesting') return t('wizard.documents.ingesting')
    if (stage === 'generating') return t('wizard.documents.generating')
    return t('common.working')
  }

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index))
  }

  // An existing collection's name IS the identifier the backend stores it
  // under, and the server's charset allows hyphens and capitals `slugify`
  // would rewrite -- so a name that already exists is passed through
  // verbatim and only a new free-text label is slugified. Slugifying it
  // pointed the page, and the upload, at a different collection that does
  // not exist (Codex review).
  const resolveKbName = (raw: string): string => {
    const trimmed = raw.trim()
    return existingKbs.some((kb) => kb.name === trimmed) ? trimmed : slugify(trimmed)
  }

  const currentSlug = resolveKbName(effectiveLabel)
  const currentKb = existingKbs.find((kb) => kb.name === currentSlug) ?? null

  // Why Remove is refused, in the reader's own terms, or null when it's
  // allowed -- the two cases `DELETE /{name}/documents/{filename}` 409s on.
  // The backend stays the authority; this only saves a pointless click.
  const removeBlockedReason = (kb: OrgKnowledgeBase, filename: string): string | null => {
    if (kb.latest_job?.status === 'queued' || kb.latest_job?.status === 'running') {
      return t('wizard.documents.removeExistingBlockedProcessing')
    }
    const readableAfter = kb.documents.filter(
      (doc) => doc.status === 'chunked' && doc.filename !== filename,
    )
    if (readableAfter.length === 0) return t('wizard.documents.removeExistingBlockedOnly')
    return null
  }

  const confirmAndRemoveExistingFile = async (kb: OrgKnowledgeBase, filename: string) => {
    const ok = await confirm({
      title: t('wizard.documents.removeExistingConfirmTitle', { name: filename }),
      // A collection can be shared: removal changes what *every* team
      // searching it can answer from, so the confirmation names them.
      body: kb.used_by.length > 0
        ? t('wizard.documents.removeExistingConfirmBodyShared', {
            kb: kb.name,
            teams: kb.used_by.join(', '),
          })
        : t('wizard.documents.removeExistingConfirmBody', { kb: kb.name }),
      confirmLabel: t('wizard.documents.removeExistingFile', { name: filename }),
      destructive: true,
    })
    if (!ok) return
    setRemovingFile(filename)
    setRemovalError(null)
    try {
      const result = await api.removeOwnKnowledgeBaseDocument(kb.name, filename)
      const job = await pollIngestionJob(kb.name, result.job_id)
      if (job === null) {
        setNotice(t('wizard.documents.stillProcessing'))
      } else if (job.status === 'failed') {
        const detail = job.errors[0]?.error
        setRemovalError(
          detail
            ? t('wizard.documents.processingFailedDetail', { detail })
            : t('wizard.documents.processingFailed'),
        )
      }
      setExistingKbs(await api.listOwnKnowledgeBases())
    } catch (e) {
      setRemovalError((e as Error).message)
    } finally {
      setRemovingFile(null)
    }
  }

  // The tail shared by a fresh upload and by clicking Continue on the
  // post-upload review panel: generate (or refine) the specification and
  // move on. `kbHintSlug` is the collection just uploaded to, or null when
  // there were no files (skip, or a review the customer opened with nothing
  // new to add).
  const finishWithSpecGeneration = async (kbHintSlug: string | null) => {
    setStage('generating')
    try {
      // Tell the architect exactly which knowledge base was just uploaded --
      // without this it only sees the org's whole KB catalog, which can
      // leave a new upload unattached (or the wrong one picked) if the org
      // already has other collections (Codex review finding).
      const kbHint = kbHintSlug
        ? `The customer just uploaded documents to a knowledge base named "${kbHintSlug}". Make sure at least one agent's tools includes it.`
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

  const continueFromReview = () => {
    const slug = reviewingSlug
    setReviewingSlug(null)
    setError(null)
    setBusy(true)
    void finishWithSpecGeneration(slug)
  }

  const proceed = async (skip: boolean) => {
    if (busy || catalogLoading || catalogUnavailable) return
    const useFiles = !skip && files.length > 0
    const slug = resolveKbName(effectiveLabel)
    // Whether this collection existed before this visit. Seeded from the
    // list fetched on load, but that request can fail or still be in flight,
    // so the name-conflict 409 below -- which proves it exists -- sets it too
    // (Codex review).
    let knownToExist = existingKbs.some((kb) => kb.name === slug)
    if (useFiles && !slug) {
      setError(t('wizard.documents.nameRequired'))
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
        uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, '', smartSearchEnabled, kbDescription)
      } catch (e) {
        const err = e as Error & { status?: number }
        if (err.status === 409) {
          // The 409 detail says what the existing collection is like today;
          // this says what it would become, so both halves of the change are
          // in the one dialog the customer has to answer. Three answers, not
          // two: adding to a collection is the common case, and before it
          // existed the only way to keep the documents already there was to
          // find them and upload them all again.
          const answer = await confirm({
            title: t('wizard.documents.existsTitle'),
            body: t('wizard.documents.existsBody', {
              detail: err.message,
              quality: smartSearchEnabled
                ? t('wizard.documents.enhanced')
                : t('wizard.documents.standard'),
            }),
            confirmLabel: t('wizard.documents.existsReplace'),
            alternateLabel: t('wizard.documents.existsAdd'),
            destructive: true,
          })
          if (!answer) {
            setBusy(false)
            setStage(null)
            return
          }
          knownToExist = true
          const mode = answer === 'alternate' ? 'add' : 'replace'
          try {
            uploadResult = await api.uploadOwnKnowledgeBaseFiles(slug, files, mode, smartSearchEnabled, kbDescription)
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
          setNotice(t('wizard.documents.stillProcessing'))
          setBusy(false)
          setStage(null)
          return
        }
        if (job.status === 'failed') {
          const detail = job.errors[0]?.error
          setError(
            detail
              ? t('wizard.documents.processingFailedDetail', { detail })
              : t('wizard.documents.processingFailed'),
          )
          setBusy(false)
          setStage(null)
          return
        }
        // A collection that already existed before this visit (detected on
        // load, or discovered just now via the name-conflict dialog above)
        // pauses here so the customer sees the merged old+new file list
        // before the page moves on -- a brand-new collection has nothing to
        // show yet, so it proceeds straight through as before.
        if (knownToExist) {
          try {
            setExistingKbs(await api.listOwnKnowledgeBases())
          } catch {
            // Best-effort refresh -- the review panel falls back to what it
            // already had rather than blocking on this.
          }
          setReviewingSlug(slug)
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

    await finishWithSpecGeneration(useFiles ? slug : null)
  }

  if (reviewingSlug) {
    const kb = existingKbs.find((k) => k.name === reviewingSlug) ?? null
    return (
      <div className="wizard-card">
        <h2>{t('wizard.documents.reviewTitle', { name: reviewingSlug })}</h2>
        {kb && kb.documents.length > 0 && (
          <ul className="tag-list" style={{ marginBottom: 16 }}>
            {kb.documents.map((doc) => (
              <li key={doc.filename}>{doc.filename}</li>
            ))}
          </ul>
        )}
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={continueFromReview}>
            {t('common.continue')}
          </button>
        </div>
        {confirmNode}
      </div>
    )
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.documents.title')}</h2>
      <p className="subtitle">{t('wizard.documents.subtitle')}</p>

      {catalogUnavailable && (
        <div className="banner banner-error">
          {catalogFailed ? t('modelCatalog.loadFailed') : t('modelCatalog.empty')}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retryCatalog}>
              {t('common.tryAgain')}
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
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      <div className="field">
        <label htmlFor="doc-label">
          {t('wizard.documents.nameLabel')}{' '}
          <span className="hint">{t('wizard.documents.nameHint')}</span>
        </label>
        <input
          id="doc-label"
          type="text"
          value={effectiveLabel}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={t('wizard.documents.namePlaceholder')}
          disabled={busy}
        />
      </div>

      {usedKbNames.length > 1 && !currentKb && (
        <div className="banner banner-info">
          <p>{t('wizard.documents.pickCollectionHint')}</p>
          <div className="wizard-actions" style={{ justifyContent: 'flex-start', gap: 8 }}>
            {usedKbNames.map((name) => (
              <button key={name} type="button" className="btn btn-secondary" onClick={() => setLabel(name)}>
                {name}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentKb && currentKb.documents.length > 0 && (
        <div className="field">
          <p className="hint">{t('wizard.documents.existingFilesTitle', { name: currentKb.name })}</p>
          {removalError && <div className="banner banner-error">{removalError}</div>}
          <ul className="tag-list" style={{ marginBottom: 16 }}>
            {currentKb.documents.map((doc) => {
              const blocked = removeBlockedReason(currentKb, doc.filename)
              return (
                <li key={doc.filename}>
                  {doc.filename}{' '}
                  <button
                    type="button"
                    onClick={() => void confirmAndRemoveExistingFile(currentKb, doc.filename)}
                    disabled={busy || removingFile !== null || blocked !== null}
                    aria-label={t('wizard.documents.removeExistingFile', { name: doc.filename })}
                    title={blocked ?? t('wizard.documents.removeExistingFile', { name: doc.filename })}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', padding: 0 }}
                  >
                    ×
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      <div className="field">
        <label htmlFor="doc-description">
          {t('wizard.documents.descriptionLabel')} <span className="hint">{t('wizard.optional')}</span>
        </label>
        <input
          id="doc-description"
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t('wizard.documents.descriptionPlaceholder')}
          maxLength={500}
          disabled={busy}
        />
        <p className="hint">{t('wizard.documents.descriptionHint')}</p>
      </div>

      {smartSearchAvailable && (
        <div className="field">
          <label>{t('wizard.documents.searchQuality')}</label>
          <div className="wizard-actions" style={{ justifyContent: 'flex-start', gap: 8, marginBottom: 4 }}>
            <button
              type="button"
              className={`btn ${smartSearch ? 'btn-secondary' : 'btn-primary'}`}
              onClick={() => setSmartSearch(false)}
              disabled={busy}
            >
              {t('wizard.documents.standard')}
            </button>
            <button
              type="button"
              className={`btn ${smartSearch ? 'btn-primary' : 'btn-secondary'}`}
              onClick={() => setSmartSearch(true)}
              disabled={busy}
            >
              {t('wizard.documents.enhanced')}
            </button>
          </div>
          <p className="hint">{t('wizard.documents.searchQualityHint')}</p>
        </div>
      )}

      <div className="upload-section">
        <label className="btn btn-secondary" style={{ display: 'inline-block' }}>
          {t('wizard.documents.chooseFiles')}
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
                aria-label={t('wizard.documents.removeFile', { name: f.name })}
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
          {t('wizard.documents.skip')}
        </button>
        <button
          className="btn btn-primary"
          onClick={() => proceed(false)}
          disabled={busy || catalogLoading || catalogUnavailable || (files.length > 0 && !currentSlug)}
        >
          {busy ? stageLabel() : t('common.continue')}
        </button>
      </div>
      {confirmNode}
    </div>
  )
}
