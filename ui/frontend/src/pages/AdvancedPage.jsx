import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import '../components/WizardLayout.css'
import './AdvancedPage.css'

const KINDS = [
  { key: 'agents', label: 'Agents', idField: 'name', editableField: 'config' },
  { key: 'teams', label: 'Teams', idField: 'name', editableField: 'config' },
  { key: 'knowledge_bases', label: 'Knowledge bases', idField: 'name', editableField: 'config' },
  { key: 'workflows', label: 'Workflows', idField: 'name', editableField: 'config' },
  { key: 'skills', label: 'Skills', idField: 'name', editableField: 'config' },
  { key: 'model-catalog', label: 'Model catalog', idField: 'spec', editableField: null },
]

function itemId(kind, item) {
  return item[kind.idField]
}

function editableJson(kind, item) {
  if (kind.editableField) return item[kind.editableField] ?? {}
  const rest = { ...item }
  delete rest[kind.idField]
  return rest
}

// "Advanced" view: raw JSON CRUD over `/api/config/...`, for fine-tuning an
// already-deployed configuration. Hidden behind its own nav entry -- the
// wizard is the primary way to build a team.
export default function AdvancedPage() {
  const [activeKey, setActiveKey] = useState(KINDS[0].key)
  const [items, setItems] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [jsonText, setJsonText] = useState('')
  const [newId, setNewId] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)
  const [saving, setSaving] = useState(false)
  const [createMode, setCreateMode] = useState('manual') // 'manual' | 'upload'
  const [uploadFiles, setUploadFiles] = useState([])
  const [uploading, setUploading] = useState(false)

  const kind = KINDS.find((k) => k.key === activeKey)
  const activeKeyRef = useRef(activeKey)

  useEffect(() => {
    activeKeyRef.current = activeKey
  }, [activeKey])

  const loadItems = () => {
    setLoading(true)
    setError(null)
    api
      .listConfig(activeKey)
      .then((data) => setItems(data))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset selection when switching resource kind
    setSelectedId(null)
    setJsonText('')
    setMessage(null)
    setNewId('')
    setCreateMode('manual')
    setUploadFiles([])
    loadItems()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey])

  const select = (id) => {
    const item = items.find((it) => itemId(kind, it) === id)
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
    try {
      const result = await api.uploadKnowledgeBaseFiles(newId.trim(), uploadFiles)
      setMessage(`Created '${result.name}' — ${result.file_count} file(s), ${result.chunk_count} chunk(s) indexed.`)
      setNewId('')
      setUploadFiles([])
      setSelectedId(result.name)
      setJsonText(JSON.stringify(result.config, null, 2))
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }

  const save = async () => {
    let parsed
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
      await api.putConfigItem(activeKey, selectedId, parsed)
      setMessage('Saved.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!selectedId) return
    const startedFor = activeKey
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await api.deleteConfigItem(activeKey, selectedId)
      setSelectedId(null)
      setJsonText('')
      setMessage('Deleted.')
      if (activeKeyRef.current === startedFor) loadItems()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="advanced">
      <header>
        <h1>Advanced configuration</h1>
        <p>Direct access to the underlying agents, teams, knowledge bases, workflows, and model catalog.</p>
      </header>

      <div className="advanced-layout">
        <nav className="advanced-kinds">
          {KINDS.map((k) => (
            <button key={k.key} className={k.key === activeKey ? 'active' : ''} onClick={() => setActiveKey(k.key)}>
              {k.label}
            </button>
          ))}
        </nav>

        <div className="advanced-list">
          {loading ? (
            <p className="hint">Loading…</p>
          ) : items.length === 0 ? (
            <p className="hint">None yet.</p>
          ) : (
            <ul>
              {items.map((item) => {
                const id = itemId(kind, item)
                return (
                  <li key={id}>
                    <button className={id === selectedId ? 'active' : ''} onClick={() => select(id)}>
                      {id}
                      {item.status && <span className="status-badge">{item.status}</span>}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}

          <div className="advanced-new">
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

            {activeKey === 'knowledge_bases' && createMode === 'upload' ? (
              <>
                <input type="text" placeholder="Knowledge base name" value={newId} onChange={(e) => setNewId(e.target.value)} />
                <input type="file" multiple onChange={(e) => setUploadFiles(Array.from(e.target.files))} />
                <button
                  className="btn btn-secondary"
                  onClick={uploadNew}
                  disabled={!newId.trim() || uploadFiles.length === 0 || uploading}
                >
                  {uploading ? 'Uploading…' : 'Create from files'}
                </button>
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
        </div>

        <div className="advanced-editor">
          {selectedId ? (
            <>
              <h2>{selectedId}</h2>
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
          ) : (
            <p className="hint">Select an item, or create a new one.</p>
          )}
        </div>
      </div>
    </div>
  )
}
