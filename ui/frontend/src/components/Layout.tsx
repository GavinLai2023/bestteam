import { useEffect } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useMe } from '../lib/useMe'
import './Layout.css'

export default function Layout() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { isAdmin } = useMe()

  // Route changes (e.g. wizard Preview -> Confirm) otherwise keep whatever
  // scroll position the previous page was at, landing the new page mid-way
  // down and hiding its heading/progress state.
  useEffect(() => {
    window.scrollTo(0, 0)
  }, [pathname])

  const logOut = () => {
    localStorage.removeItem('bestteam_token')
    navigate('/login')
  }

  return (
    <div className="app-shell">
      <nav className="top-nav">
        <span className="brand">bestteam</span>
        <div className="top-nav-links">
          {/* Customer pages are org-scoped; a platform operator (no org) can't
              use them, so show them only to org members. */}
          {!isAdmin && (
            <>
              <NavLink to="/activity" className={({ isActive }) => (isActive ? 'active' : '')}>
                Dashboard
              </NavLink>
              <NavLink to="/wizard" className={({ isActive }) => (isActive ? 'active' : '')}>
                Build a team
              </NavLink>
              <NavLink to="/teams" className={({ isActive }) => (isActive ? 'active' : '')}>
                My teams
              </NavLink>
              <NavLink to="/run" className={({ isActive }) => (isActive ? 'active' : '')}>
                Run a team
              </NavLink>
            </>
          )}
          {isAdmin && (
            <>
              <NavLink to="/accounts" className={({ isActive }) => (isActive ? 'active' : '')}>
                Accounts
              </NavLink>
              <NavLink to="/advanced" className={({ isActive }) => (isActive ? 'active' : '')}>
                Advanced
              </NavLink>
              <NavLink to="/memory" className={({ isActive }) => (isActive ? 'active' : '')}>
                Memory
              </NavLink>
            </>
          )}
          <button type="button" className="logout-button" onClick={logOut}>
            Log out
          </button>
        </div>
      </nav>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  )
}
