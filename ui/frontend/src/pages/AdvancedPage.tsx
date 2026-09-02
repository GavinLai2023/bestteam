import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { visibleOrgOptions } from '../lib/orgs'
import { useConfirm } from '../lib/useConfirm'
import type { AdminOrg, ConfigItem, SkillReference, SkillVersionInfo } from '../lib/types'
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
// then read-only reference. A pipeline is what the wizard and the customer UI
// call an "AI team"; this page is operator-only, so it uses the noun that
// matches the JSON keys, the API path, and the YAML.
const KINDS: Kind[] = [
  { key: 'pipelines', label: 'Pipelines', idField: 'name', editableField: 'config', orgScope: 'required' },
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
  const [confirmNode, confirm] = useConfirm()
  const { t } = useTranslation()
  // An org with dozens of knowledge bases had no way to find one but to scan
  // the list by eye (audit finding F15).
  const [filter, setFilter] = useState('')
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
  const [showInactiveOrgs, setShowInactiveOrgs] = useState(false)
  // Skills tab only: the selected skill's immutable version history, the
  // deployed teams pinning it, and which historical version (if any) is being
  // viewed read-only. null = the editable head.
  const [skillVersions, setSkillVersions] = useState<SkillVersionInfo[]>([])
  const [skillReferences, setSkillReferences] = useState<SkillReference[]>([])
  const [viewVersion, setViewVersion] = useState<number | null>(null)
  // Target organisation for "Copy to organisation" on a locked built-in.
  const [copyOrg, setCopyOrg] = useState('')

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
  // A platform built-in is locked here (seeding owns its content); an org
  // copy or org skill stays editable. Viewing a historical version is always
  // read-only -- the history is immutable by design.
  const isBuiltinSkill = activeKey === 'skills' && selectedItem?.builtin === true
  const viewedVersion =
    viewVersion == null ? null : skillVersions.find((v) => v.version === viewVersion) ?? null
  const editorReadOnly = isBuiltinSkill || viewedVersion != null
  const editorText = viewedVersion ? JSON.stringify(viewedVersion.config, null, 2) : jsonText
  // Filtering is display-only: `visibleItems` remains the org-scoping decision
  // above, so a hidden row can never become a mutation target.
  const filteredItems = filter.trim()
    ? visibleItems.filter((it) => itemId(kind, it).toLowerCase().includes(filter.trim().toLowerCase()))
    : visibleItems

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
    setFilter('')
    setSkillVersions([])
    setSkillReferences([])
    setViewVersion(null)
    setCopyOrg('')
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
    setViewVersion(null)
    setCopyOrg('')
    if (activeKey === 'skills' && item) {
      // Best-effort side panels: the editor must keep working if either
      // lookup fails (e.g. a just-created, not-yet-saved skill has neither).
      setSkillVersions([])
      setSkillReferences([])
      api.skillVersions(id, apiOrg).then(setSkillVersions).catch(() => {})
      api.skillReferences(id, apiOrg).then(setSkillReferences).catch(() => {})
    }
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
    } catch (e) {
      // The parser's own message names the position, which is the only way to
      // find the problem in an 18-row document.
      setError(t('advanced.invalidJson', { detail: (e as Error).message }))
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
      if (activeKey === 'skills' && selectedId) {
        api.skillVersions(selectedId, apiOrg).then(setSkillVersions).catch(() => {})
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  // Locked built-ins can't be edited in place; customisation is a copy into
  // an organisation, where the same-named copy shadows the built-in on that
  // org's next deploy (load_skills' fold order).
  const copyToOrg = async () => {
    if (!selectedId || !copyOrg) return
    let parsed: ConfigItem
    try {
      parsed = JSON.parse(jsonText)
    } catch (e) {
      setError(t('advanced.invalidJson', { detail: (e as Error).message }))
      return
    }
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.putConfigItem('skills', selectedId, parsed, copyOrg)
      setMessage(
        `Copied to ${copyOrg}. The copy shadows the built-in on that organisation's next deploy.`,
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!selectedId) return
    const ok = await confirm({
      title: `Delete "${selectedId}"?`,
      body: `It will be removed from ${kind.label}. This cannot be undone.`,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
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
        <p>Direct access to the underlying pipelines, skills, knowledge bases, tools, and model catalog.</p>
        {kind.orgScope !== 'none' && (
          <label className="advanced-org">
            Organisation
            <select value={org ?? ''} onChange={(e) => selectOrg(e.target.value)}>
              {kind.orgScope === 'optional' && <option value={PLATFORM_TIER}>Platform (built-ins)</option>}
              {visibleOrgOptions(orgs, showInactiveOrgs, org).map((o) => (
                <option key={o.name} value={o.name}>
                  {o.display_name || o.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {kind.orgScope !== 'none' && orgs.some((o) => !o.active) && (
          <label className="advanced-org-inactive">
            <input
              type="checkbox"
              checked={showInactiveOrgs}
              onChange={(e) => setShowInactiveOrgs(e.target.checked)}
            />
            Show deactivated
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
          {!loading && visibleItems.length > 0 && (
            <input
              type="search"
              className="advanced-filter"
              placeholder={t('advanced.filterPlaceholder')}
              aria-label={t('advanced.filterPlaceholder')}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          )}
          {loading ? (
            <p className="hint">{t('common.loading')}</p>
          ) : visibleItems.length === 0 ? (
            <p className="hint">None yet.</p>
          ) : filteredItems.length === 0 ? (
            <p className="hint">{t('advanced.noMatches')}</p>
          ) : (
            <ul>
              {filteredItems.map((item) => {
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
                {activeKey === 'skills' && skillVersions.length > 0 ? (
                  <select
                    className="advanced-version-select"
                    aria-label="Version"
                    value={viewVersion ?? 'head'}
                    onChange={(e) =>
                      setViewVersion(e.target.value === 'head' ? null : Number(e.target.value))
                    }
                  >
                    {skillVersions.map((v) => (
                      <option key={v.version} value={v.current ? 'head' : v.version}>
                        v{v.version}
                        {v.current ? ' (current)' : ''}
                      </option>
                    ))}
                  </select>
                ) : (
                  activeKey === 'skills' && selectedItem?.version != null && ` · v${selectedItem.version}`
                )}
              </h2>
              {activeKey === 'skills' && !isBuiltinSkill && (
                <p className="hint">
                  Saving appends a version. Deployed teams keep their pinned version until you redeploy them.
                </p>
              )}
              {isBuiltinSkill && (
                <p className="hint">
                  Platform built-in — updated by platform releases and locked here. To customise it
                  for one organisation, copy it: the organisation&apos;s copy shadows the built-in
                  the next time a team is deployed.
                </p>
              )}
              {viewedVersion && (
                <p className="hint">
                  Historical version — read-only. Deployed teams pinned to it keep receiving exactly
                  this content.
                </p>
              )}
              {error && <p className="banner banner-error">{error}</p>}
              {message && <p className="banner banner-success">{message}</p>}
              <textarea
                rows={18}
                value={editorText}
                onChange={(e) => {
                  if (!editorReadOnly) setJsonText(e.target.value)
                }}
                readOnly={editorReadOnly}
                spellCheck={false}
              />
              {isBuiltinSkill ? (
                <div className="wizard-actions">
                  <select
                    aria-label="Copy to organisation"
                    value={copyOrg}
                    onChange={(e) => setCopyOrg(e.target.value)}
                  >
                    <option value="">Choose organisation…</option>
                    {orgs
                      .filter((o) => o.active)
                      .map((o) => (
                        <option key={o.name} value={o.name}>
                          {o.display_name || o.name}
                        </option>
                      ))}
                  </select>
                  <button
                    className="btn btn-secondary"
                    onClick={copyToOrg}
                    disabled={!copyOrg || saving}
                  >
                    Copy to organisation
                  </button>
                </div>
              ) : (
                <div className="wizard-actions">
                  <button
                    className="btn btn-primary"
                    onClick={save}
                    disabled={saving || viewedVersion != null}
                  >
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                  {/* Never the same visual weight as Save: this one is not
                      reachable by muscle memory (F15). */}
                  <button className="btn btn-danger-outline" onClick={remove} disabled={saving}>
                    Delete
                  </button>
                </div>
              )}
              {activeKey === 'skills' && (
                <div className="advanced-skill-references">
                  <h3>Referenced by deployed teams</h3>
                  {skillReferences.length === 0 ? (
                    <p className="hint">No deployments reference this skill.</p>
                  ) : (
                    <ul>
                      {skillReferences.map((r, i) => (
                        <li key={i}>
                          {(r.org_display_name ?? 'platform') + ' · ' + r.pipeline_name}
                          {' · pinned v' + String(r.pinned_version ?? '?') + ' · '}
                          {r.is_current_deploy ? 'current deploy' : 'superseded version'}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      {confirmNode}
    </div>
  )
}
