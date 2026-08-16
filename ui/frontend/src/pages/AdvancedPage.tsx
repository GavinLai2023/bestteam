import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { AdminOrg, ConfigItem } from '../lib/types'
import '../components/WizardLayout.css'
import './AdvancedPage.css'

// `orgScope` mirrors what the backend requires on each item route:
//   required -- ?org= or 422 (crud.py::_resolve_org_id)
//   optional -- omitted means the platform built-in tier (skills only)
//   none     -- resource isn't org-scoped at all; hide the selector
const PLATFORM_TIER = '__platform__'

// Knowledge-base upload polling, capped at ~1 minute: nothing reconciles a
// queued/running IngestionJob left behind by a backend restart, so an
// uncapped loop would poll forever.
const INGESTION_POLL_INTERVAL_MS = 500
const INGESTION_POLL_MAX_ATTEMPTS = 120

interface Kind {
  key: string
  label: string
  idField: string
  editableField: string | null
  orgScope: 'required' | 'optional' | 'none'
  readOnly?: boolean
}

// Ordered whole-then-parts: the deployable unit, then what it's built from,
// then read-only reference. A workflow is what the wizard and the customer UI
// call an "AI team"; this page is operator-only, so it uses the noun that
// matches the JSON keys, the API path, and the YAML.
const KINDS: Kind[] = [
  { key: 'workflows', label: 'Workflows', idField: 'name', editableField: 'config', orgScope: 'required' },
  { key: 'skills', label: 'Skills', idField: 'name', editableField: 'config', orgScope: 'optional' },
  { key: 'knowledge_bases', label: 'Knowledge bases', idField: 'name', editableField: 'config', orgScope: 'required' },
  { key: 'tools', label: 'Tools', idField: 'name', editableField: null, orgScope: 'none', readOnly: true },
  { key: 'model-catalog', label: 'Model catalog', idField: 'spec', editableField: null, orgScope: 'none' },
]

function itemId(kind: Kind, item: ConfigItem): string {
  return String(item[kind.idField])
}

function defaultOrgFor(kind: Kind, orgs: AdminOrg[]): string | null {
  if (kind.orgScope === 'none') return null
  if (kind.orgScope === 'optional') return PLATFORM_TIER
  return orgs.length ? orgs[0].name : null
}

function editableJson(kind: Kind, item: ConfigItem): ConfigItem {
  if (kind.editableField) return (item[kind.editableField] as ConfigItem) ?? {}
  const rest = { ...item }
  delete rest[kind.idField]
  return rest
}

// "Advanced" view: raw JSON CRUD over `/api/config/...`, for fine-tuning an
// already-deployed configuration. Hidden behind its own nav entry -- the
// wizard is the primary way to build a team.
export default function AdvancedPage() {
  const [activeKey, setActiveKey] = useState(KINDS[0].key)
  const [items, setItems] = useState<ConfigItem[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [jsonText, setJsonText] = useState('')
  const [newId, setNewId] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [createMode, setCreateMode] = useState<'manual' | 'upload'>('manual')
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  // "Uploaded, still processing" -- neither success nor failure, and it has
  // to render next to the upload form itself, which is visible with no item
  // selected (unlike `error`/`message`, which live in the editor pane).
  const [uploadNotice, setUploadNotice] = useState<string | null>(null)
  const [orgs, setOrgs] = useState<AdminOrg[]>([])
  const [org, setOrg] = useState<string | null>(null)

  const kind = KINDS.find((k) => k.key === activeKey)!
  const activeKeyRef = useRef(activeKey)
  // The last organisation the user explicitly selected (never the platform
  // tier). Used to restore their choice when switching to an
  // organisation-required tab from one where the platform tier was selected.
  const lastRealOrgRef = useRef<string | null>(null)
  // Monotonic load token: a list response is only applied if it's the most
  // recent request, so a slow response for a previous org/tab can't overwrite
  // the current one (and can't leave a stale item selectable for a mutation
  // that would then target the wrong org).
  const loadSeq = useRef(0)

  // What actually goes on the wire: the platform tier is expressed by omitting
  // `?org=` entirely, and org-less resources never send it.
  const apiOrg = kind.orgScope === 'none' || org === PLATFORM_TIER ? undefined : (org ?? undefined)

  // The skills list can't ask the API for "built-ins only" -- omitting ?org=
  // means unfiltered there, so it returns every org's skills. Narrow to the
  // platform tier here, otherwise saving a listed org skill would silently
  // write a built-in copy of it instead.
  const visibleItems =
    kind.orgScope === 'optional' && org === PLATFORM_TIER
      ? items.filter((it) => it.org == null)
      : items
  const selectedItem = visibleItems.find((it) => itemId(kind, it) === selectedId)

  useEffect(() => {
    activeKeyRef.current = activeKey
  }, [activeKey])

  useEffect(() => {
    if (org && org !== PLATFORM_TIER) lastRealOrgRef.current = org
  }, [org])

  const loadItems = () => {
    const seq = ++loadSeq.current
    setLoading(true)
    setError(null)
    api
      .listConfig(activeKey, apiOrg)
      .then((data) => {
        if (seq === loadSeq.current) setItems(data)
      })
      .catch((e: Error) => {
        if (seq === loadSeq.current) setError(e.message)
      })
      .finally(() => {
        if (seq === loadSeq.current) setLoading(false)
      })
  }

  useEffect(() => {
    // Orgs arrive after first paint, so the initial tab has no org to target
    // until they do; pick its default here rather than in a cascading effect.
    api
      .listOrgs()
      .then((data) => {
        setOrgs(data)
        setOrg((current) => current ?? defaultOrgFor(kind, data))
      })
      .catch((e: Error) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (kind.orgScope === 'required' && !org) return // still waiting on listOrgs
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/org change
    loadItems()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, org])

  // Drop any open editor/selection. Both switching tab and switching org must
  // do this: otherwise the editor keeps the previous context's item id + JSON,
  // and a Save/Delete would then create or destroy that item in the newly
  // selected org (a cross-tenant write). See selectKind / selectOrg.
  const resetSelection = () => {
    setSelectedId(null)
    setJsonText('')
    setMessage(null)
    setError(null)
    setNewId('')
    setCreateMode('manual')
    setUploadFiles([])
  }

  // Switching tabs keeps whatever organisation the user has selected -- it
  // should never silently jump back to a default. The one exception: the
  // platform tier (only offered on the Skills tab) isn't a valid choice on an
  // organisation-required tab, so that case falls back to the last real
  // organisation the user picked, or the usual default if there isn't one.
  const selectKind = (k: Kind) => {
    if (k.key === activeKey) return
    setActiveKey(k.key)
    if (k.orgScope === 'required' && (org === null || org === PLATFORM_TIER)) {
      setOrg(lastRealOrgRef.current ?? defaultOrgFor(k, orgs))
    }
    resetSelection()
  }

  const selectOrg = (value: string) => {
    if (value === org) return
    setOrg(value)
    resetSelection()
  }

  const select = (id: string) => {
    const item = visibleItems.find((it) => itemId(kind, it) === id)
    setSelectedId(id)
    setMessage(null)
    setError(null)
    setJsonText(JSON.stringify(item ? editableJson(kind, item) : {}, null, 2))
  }

  const startNew = () => {
    if (!newId.trim()) return
    setSelectedId(newId.trim())
    setNewId('')
    setMessage(null)
    setError(null)
    setJsonText('{\n  \n}')
  }

  const uploadNew = async () => {
    if (!newId.trim() || uploadFiles.length === 0) return
    const startedFor = activeKey
    setUploading(true)
    setError(null)
    setMessage(null)
    setUploadNotice(null)
    try {
      const uploadResult = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles, apiOrg)
      let job = await api.knowledgeBaseUploadJob(uploadResult.name, uploadResult.job_id, apiOrg)
      for (
        let attempt = 0;
        attempt < INGESTION_POLL_MAX_ATTEMPTS && job.status !== 'completed' && job.status !== 'failed';
        attempt++
      ) {
        await new Promise((resolve) => setTimeout(resolve, INGESTION_POLL_INTERVAL_MS))
        job = await api.knowledgeBaseUploadJob(uploadResult.name, uploadResult.job_id, apiOrg)
      }
      if (job.status !== 'completed' && job.status !== 'failed') {
        // Bounded rather than spinning forever: nothing reconciles a
        // queued/running job stranded by a backend restart. The upload itself
        // succeeded, so report "still processing", not success or failure.
        setUploadNotice(
          `'${uploadResult.name}' was uploaded but is still being processed — this is taking longer ` +
            'than expected. Reload this page in a moment to check on it.',
        )
        if (activeKeyRef.current === startedFor) loadItems()
        return
      }
      if (job.status === 'failed') {
        const detail = job.errors[0]?.error
        throw new Error(detail ? `Processing failed: ${detail}` : 'Processing your documents failed.')
      }
      setMessage(`Created '${uploadResult.name}' — ${job.documents_succeeded} file(s), ${job.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(uploadResult.name)
      setJsonText(JSON.stringify(job.config, null, 2))
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const save = async () => {
    let parsed: ConfigItem
    try {
      parsed = JSON.parse(jsonText)
    } catch {
      setError('Not valid JSON')
      return
    }

    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.putConfigItem(activeKey, selectedId!, parsed, apiOrg)
      setMessage('Saved.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!selectedId) return
    if (!window.confirm(`Delete "${selectedId}" from ${kind.label}? This cannot be undone.`)) return
    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.deleteConfigItem(activeKey, selectedId, apiOrg)
      setSelectedId(null)
      setJsonText('')
      setMessage('Deleted.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="advanced">
      <header>
        <h1>Advanced configuration</h1>
        <p>Direct access to the underlying workflows, skills, knowledge bases, tools, and model catalog.</p>
        {kind.orgScope !== 'none' && (
          <label className="advanced-org">
            Organisation
            <select value={org ?? ''} onChange={(e) => selectOrg(e.target.value)}>
              {kind.orgScope === 'optional' && <option value={PLATFORM_TIER}>Platform (built-ins)</option>}
              {orgs.map((o) => (
                <option key={o.name} value={o.name}>
                  {o.display_name || o.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>

      <div className="advanced-layout">
        <nav className="advanced-kinds">
          {KINDS.map((k) => (
            <button key={k.key} className={k.key === activeKey ? 'active' : ''} onClick={() => selectKind(k)}>
              {k.label}
            </button>
          ))}
        </nav>

        <div className="advanced-list">
          {loading ? (
            <p className="hint">Loading…</p>
          ) : visibleItems.length === 0 ? (
            <p className="hint">None yet.</p>
          ) : (
            <ul>
              {visibleItems.map((item) => {
                const id = itemId(kind, item)
                return (
                  <li key={id}>
                    <button className={id === selectedId ? 'active' : ''} onClick={() => select(id)}>
                      {id}
                      {(item.status as string) && <span className="status-badge">{item.status as string}</span>}
                      {activeKey === 'skills' && item.version != null && (
                        <span className="status-badge">v{item.version as string | number}</span>
                      )}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}

          {activeKey === 'knowledge_bases' && (
            <div className="advanced-create-mode">
              <button className={createMode === 'manual' ? 'active' : ''} onClick={() => setCreateMode('manual')}>
                Manual JSON
              </button>
              <button className={createMode === 'upload' ? 'active' : ''} onClick={() => setCreateMode('upload')}>
                Upload files
              </button>
            </div>
          )}

          {!kind.readOnly && (
            <div className="advanced-new">
              {activeKey === 'knowledge_bases' && createMode === 'upload' ? (
                <>
                  <input type="text" placeholder="Knowledge base name" value={newId} onChange={(e) => setNewId(e.target.value)} />
                  <input type="file" multiple onChange={(e) => setUploadFiles(Array.from(e.target.files!))} />
                  <button
                    className="btn btn-secondary"
                    onClick={uploadNew}
                    disabled={!newId.trim() || uploadFiles.length === 0 || uploading}
                  >
                    {uploading ? 'Uploading…' : 'Create from files'}
                  </button>
                  {uploadNotice && <p className="banner banner-info">{uploadNotice}</p>}
                </>
              ) : (
                <>
                  <input type="text" placeholder={`New ${kind.idField}`} value={newId} onChange={(e) => setNewId(e.target.value)} />
                  <button className="btn btn-secondary" onClick={startNew} disabled={!newId.trim()}>
                    New
                  </button>
                </>
              )}
            </div>
          )}
        </div>

        <div className="advanced-editor">
          {!selectedId ? (
            <p className="hint">{kind.readOnly ? 'Select a tool.' : 'Select an item, or create a new one.'}</p>
          ) : kind.readOnly ? (
            <>
              <h2>{selectedId}</h2>
              <p className="advanced-readonly-text">
                {visibleItems.find((it) => itemId(kind, it) === selectedId)?.description as string}
              </p>
              <p className="hint">
                Built-in tool. Reference it by this name in an agent&apos;s or skill&apos;s <code>tools</code> list.
              </p>
            </>
          ) : (
            <>
              <h2>
                {selectedId}
                {activeKey === 'skills' && selectedItem?.version != null && ` · v${selectedItem.version}`}
              </h2>
              {activeKey === 'skills' && (
                <p className="hint">
                  Saving appends a version. Deployed teams keep their pinned version until you redeploy them.
                </p>
              )}
              {error && <p className="banner banner-error">{error}</p>}
              {message && <p className="banner banner-success">{message}</p>}
              <textarea rows={18} value={jsonText} onChange={(e) => setJsonText(e.target.value)} spellCheck={false} />
              <div className="wizard-actions">
                <button className="btn btn-primary" onClick={save} disabled={saving}>
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button className="btn btn-secondary" onClick={remove} disabled={saving}>
                  Delete
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
