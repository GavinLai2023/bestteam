import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import '../components/WizardLayout.css'
import './AdvancedPage.css'

const TYPES = ['episodic', 'semantic', 'procedural']

// Admin-only view of the per-user memory store: pick a user, browse/search
// their remembered records, delete a bad one, or clear their whole memory.
// Memory is opt-in (BESTTEAM_MEMORY_DB); when disabled the API reports
// enabled:false and this page shows a clear "not enabled" notice.
export default function MemoryPage() {
  const [enabled, setEnabled] = useState(true)
  const [users, setUsers] = useState([])
  // A selected summary is an identity: {user_id, org_id} (org_id null = legacy).
  // A username can appear under several orgs (a moved user), so selection and
  // React keys must use both fields, not user_id alone.
  const [selected, setSelected] = useState(null)
  const [records, setRecords] = useState([])
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  const loadUsers = () => {
    setLoading(true)
    setError(null)
    api
      .memoryUsers()
      .then((data) => {
        setEnabled(data.enabled)
        setUsers(data.users)
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    loadUsers()
  }, [])

  const loadRecords = (identity, opts = {}) => {
    if (!identity) return
    setError(null)
    api
      .memoryRecords(identity.user_id, {
        query: opts.query ?? query,
        type: opts.type ?? typeFilter,
        org: identity.org_id,
      })
      .then((data) => setRecords(data.records))
      .catch((e) => setError(e.message))
  }

  const selectIdentity = (identity) => {
    setSelected(identity)
    setMessage(null)
    setError(null)
    setQuery('')
    setTypeFilter('')
    loadRecords(identity, { query: '', type: '' })
  }

  const filterByType = (type) => {
    setTypeFilter(type)
    loadRecords(selected, { type })
  }

  const submitSearch = (e) => {
    e.preventDefault()
    loadRecords(selected)
  }

  const sameIdentity = (a, b) => a && b && a.user_id === b.user_id && a.org_id === b.org_id
  const scopeLabel = (orgId) => (orgId == null ? 'legacy (no org)' : `org ${orgId}`)

  const deleteRecord = async (id) => {
    if (!window.confirm('Delete this memory record? This cannot be undone.')) return
    setError(null)
    setMessage(null)
    try {
      await api.deleteMemoryRecord(id)
      setRecords((prev) => prev.filter((r) => r.id !== id))
      setMessage('Record deleted.')
      loadUsers()
    } catch (e) {
      setError(e.message)
    }
  }

  const clearUser = async () => {
    if (!selected) return
    const name = selected.user_id
    if (!window.confirm(`Clear ALL memory for "${name}" (every organization)? This cannot be undone.`))
      return
    setError(null)
    setMessage(null)
    try {
      const result = await api.clearUserMemory(name)
      setRecords([])
      setMessage(`Cleared ${result.removed} record(s) for ${name}.`)
      setSelected(null)
      loadUsers()
    } catch (e) {
      setError(e.message)
    }
  }

  if (!loading && !enabled) {
    return (
      <div className="advanced">
        <header>
          <h1>Per-user memory</h1>
        </header>
        <p className="banner banner-error">
          Memory is not enabled on this deployment. Set <code>BESTTEAM_MEMORY_DB</code> to a database
          path to enable per-user memory, then restart the backend.
        </p>
      </div>
    )
  }

  return (
    <div className="advanced">
      <header>
        <h1>Per-user memory</h1>
        <p>Inspect and manage what the platform remembers about each user across sessions.</p>
      </header>

      <div className="advanced-layout memory-layout">
        <div className="advanced-list">
          {loading ? (
            <p className="hint">Loading…</p>
          ) : users.length === 0 ? (
            <p className="hint">No users have any memory yet.</p>
          ) : (
            <ul>
              {users.map((u) => {
                const identity = { user_id: u.user_id, org_id: u.org_id }
                return (
                  <li key={`${u.org_id}:${u.user_id}`}>
                    <button
                      className={sameIdentity(identity, selected) ? 'active' : ''}
                      onClick={() => selectIdentity(identity)}
                    >
                      {u.user_id}
                      <span className="hint"> · {scopeLabel(u.org_id)}</span>
                      <span className="status-badge">{u.total}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="advanced-editor">
          {selected ? (
            <>
              <h2>
                {selected.user_id} <span className="hint">· {scopeLabel(selected.org_id)}</span>
              </h2>
              {error && <p className="banner banner-error">{error}</p>}
              {message && <p className="banner banner-success">{message}</p>}

              <form className="wizard-actions" onSubmit={submitSearch}>
                <input
                  type="text"
                  placeholder="Search records…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <button className="btn btn-secondary" type="submit">
                  Search
                </button>
              </form>

              <div className="advanced-create-mode">
                <button className={typeFilter === '' ? 'active' : ''} onClick={() => filterByType('')}>
                  All
                </button>
                {TYPES.map((t) => (
                  <button key={t} className={typeFilter === t ? 'active' : ''} onClick={() => filterByType(t)}>
                    {t}
                  </button>
                ))}
              </div>

              {records.length === 0 ? (
                <p className="hint">No matching records.</p>
              ) : (
                <ul className="memory-records">
                  {records.map((r) => (
                    <li key={r.id} className="memory-record">
                      <div className="memory-record-head">
                        <span className="status-badge">{r.type}</span>
                        <span className="hint">{r.created_at}</span>
                        <button className="btn btn-secondary" onClick={() => deleteRecord(r.id)}>
                          Delete
                        </button>
                      </div>
                      <p className="memory-record-content">{r.content}</p>
                    </li>
                  ))}
                </ul>
              )}

              <div className="wizard-actions">
                <button className="btn btn-secondary" onClick={clearUser}>
                  Clear all memory for {selected.user_id} (every org)
                </button>
              </div>
            </>
          ) : (
            <p className="hint">Select a user to view their memory.</p>
          )}
        </div>
      </div>
    </div>
  )
}
