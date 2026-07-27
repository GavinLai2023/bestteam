import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import '../components/WizardLayout.css'
import './AdvancedPage.css'
import './AccountsPage.css'

// Admin-only org/user management: create orgs, deactivate/reactivate them, and
// manage each org's single member login. Granting admin and the whole
// platform-account lifecycle stay CLI-only, so platform accounts are shown
// read-only. The backend enforces admin on every /api/admin call regardless.
export default function AccountsPage() {
  const [orgs, setOrgs] = useState([])
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  const [newOrgName, setNewOrgName] = useState('')
  const [newOrgDisplay, setNewOrgDisplay] = useState('')
  // Per-org create-user drafts, keyed by org name: { [org]: {username, password} }.
  const [drafts, setDrafts] = useState({})

  const reload = () =>
    Promise.all([api.adminOrgs(), api.adminUsers()]).then(([o, u]) => {
      setOrgs(o)
      setUsers(u)
    })

  useEffect(() => {
    reload()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  // Resolves to true on success, false on failure -- callers clear their form
  // only on true, so a failed create keeps the entered values (review r-ext2 #5).
  const run = (promise, okMessage) => {
    setError(null)
    setMessage(null)
    return promise
      .then(() => reload())
      .then(() => {
        if (okMessage) setMessage(okMessage)
        return true
      })
      .catch((e) => {
        setError(e.message)
        return false
      })
  }

  const createOrg = (e) => {
    e.preventDefault()
    if (!newOrgName.trim()) return
    run(api.createAdminOrg(newOrgName.trim(), newOrgDisplay.trim()), `Created '${newOrgName.trim()}'.`).then(
      (ok) => {
        if (ok) {
          setNewOrgName('')
          setNewOrgDisplay('')
        }
      },
    )
  }

  const toggleActive = (org) => {
    if (org.active && !window.confirm(`Deactivate '${org.name}'? Its user won't be able to log in.`)) return
    run(api.setOrgActive(org.name, !org.active))
  }

  const emptyDraft = { username: '', password: '', confirm: '' }
  const draftFor = (org) => drafts[org] || emptyDraft
  const setDraft = (org, patch) =>
    setDrafts((d) => ({ ...d, [org]: { ...draftFor(org), ...patch } }))

  const createUser = (e, org) => {
    e.preventDefault()
    const { username, password, confirm } = draftFor(org)
    if (!username.trim() || !password) return
    if (password !== confirm) {
      setMessage(null)
      setError('Passwords do not match.')
      return
    }
    run(api.createAdminUser(username.trim(), org, password)).then((ok) => {
      if (ok) setDrafts((d) => ({ ...d, [org]: emptyDraft }))
    })
  }

  const resetPassword = (username) => {
    const pw = window.prompt(`New password for '${username}'`)
    if (!pw) return
    run(api.resetAdminUserPassword(username, pw), `Password reset for '${username}'.`)
  }

  const moveUser = (username) => {
    const to = window.prompt(`Move '${username}' to which organization?`)
    if (!to || !to.trim()) return
    run(api.moveAdminUser(username, to.trim()))
  }

  const removeUser = (username) => {
    if (!window.confirm(`Delete user '${username}'? This also purges their memory.`)) return
    run(api.deleteAdminUser(username), `Deleted '${username}'.`)
  }

  const platformAccounts = users.filter((u) => u.org === null)

  if (loading) return null

  return (
    <div className="advanced">
      <header>
        <h1>Organizations &amp; users</h1>
        <p>Create organizations, suspend them, and manage each org&apos;s login.</p>
      </header>

      {error && <p className="banner banner-error">{error}</p>}
      {message && <p className="banner banner-success">{message}</p>}

      <section>
        <h2>Organizations</h2>

        <form onSubmit={createOrg} className="inline-form">
          <label htmlFor="new-org-name">Organization name</label>
          <input
            id="new-org-name"
            value={newOrgName}
            onChange={(e) => setNewOrgName(e.target.value)}
          />
          <label htmlFor="new-org-display">Display name</label>
          <input
            id="new-org-display"
            value={newOrgDisplay}
            onChange={(e) => setNewOrgDisplay(e.target.value)}
          />
          <button type="submit" className="btn btn-primary">
            Create organization
          </button>
        </form>

        <ul className="org-list">
          {orgs.map((org) => (
            <li key={org.name} className="org-row">
              <span className="org-name">{org.display_name || org.name}</span>
              <span className={`badge ${org.active ? 'badge-active' : 'badge-inactive'}`}>
                {org.active ? 'Active' : 'Deactivated'}
              </span>
              <span className="org-member">{org.member ? org.member : 'no member yet'}</span>

              <button type="button" className="btn" onClick={() => toggleActive(org)}>
                {org.active ? 'Deactivate' : 'Reactivate'}
              </button>

              {org.member ? (
                <>
                  <button type="button" className="btn" onClick={() => resetPassword(org.member)}>
                    Reset password
                  </button>
                  <button type="button" className="btn" onClick={() => moveUser(org.member)}>
                    Move
                  </button>
                  <button type="button" className="btn btn-danger" onClick={() => removeUser(org.member)}>
                    Delete
                  </button>
                </>
              ) : (
                <form onSubmit={(e) => createUser(e, org.name)} className="inline-form">
                  <input
                    aria-label={`Username for ${org.name}`}
                    value={draftFor(org.name).username}
                    onChange={(e) => setDraft(org.name, { username: e.target.value })}
                    placeholder="username"
                  />
                  <input
                    aria-label={`Password for ${org.name}`}
                    type="password"
                    value={draftFor(org.name).password}
                    onChange={(e) => setDraft(org.name, { password: e.target.value })}
                    placeholder="password"
                  />
                  <input
                    aria-label={`Confirm password for ${org.name}`}
                    type="password"
                    value={draftFor(org.name).confirm}
                    onChange={(e) => setDraft(org.name, { confirm: e.target.value })}
                    placeholder="confirm password"
                  />
                  <button type="submit" className="btn btn-primary">
                    Create user
                  </button>
                </form>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Platform accounts</h2>
        <p className="hint">Operators and admins are managed via the CLI.</p>
        <ul className="org-list">
          {platformAccounts.map((u) => (
            <li key={u.username} className="org-row">
              <span className="org-name">{u.username}</span>
              {u.is_admin && <span className="badge badge-active">admin</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
