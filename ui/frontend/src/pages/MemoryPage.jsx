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
  const [selectedUser, setSelectedUser] = useState(null)
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

  const loadRecords = (userId, opts = {}) => {
    setError(null)
    api
      .memoryRecords(userId, { query: opts.query ?? query, type: opts.type ?? typeFilter })
      .then((data) => setRecords(data.records))
      .catch((e) => setError(e.message))
  }

  const selectUser = (userId) => {
    setSelectedUser(userId)
    setMessage(null)
    setError(null)
    setQuery('')
    setTypeFilter('')
    loadRecords(userId, { query: '', type: '' })
  }

  const filterByType = (type) => {
    setTypeFilter(type)
    loadRecords(selectedUser, { type })
  }

  const submitSearch = (e) => {
    e.preventDefault()
    loadRecords(selectedUser)
  }

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
    if (!selectedUser) return
    if (!window.confirm(`Clear ALL memory for "${selectedUser}"? This cannot be undone.`)) return
    setError(null)
    setMessage(null)
    try {
      const result = await api.clearUserMemory(selectedUser)
      setRecords([])
      setMessage(`Cleared ${result.removed} record(s) for ${selectedUser}.`)
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
              {users.map((u) => (
                <li key={u.user_id}>
                  <button className={u.user_id === selectedUser ? 'active' : ''} onClick={() => selectUser(u.user_id)}>
                    {u.user_id}
                    <span className="status-badge">{u.total}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="advanced-editor">
          {selectedUser ? (
            <>
              <h2>{selectedUser}</h2>
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
                  Clear all memory for {selectedUser}
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
