import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useMe } from '../lib/useMe'
import './Layout.css'

export default function Layout() {
  const navigate = useNavigate()
  const { isAdmin } = useMe()

  const logOut = () => {
    localStorage.removeItem('bestteam_token')
    navigate('/login')
  }

  return (
    <div className="app-shell">
      <nav className="top-nav">
        <span className="brand">bestteam</span>
        <div className="top-nav-links">
          <NavLink to="/wizard" className={({ isActive }) => (isActive ? 'active' : '')}>
            Build a team
          </NavLink>
          <NavLink to="/teams" className={({ isActive }) => (isActive ? 'active' : '')}>
            My teams
          </NavLink>
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
            Talk to your team
          </NavLink>
          {isAdmin && (
            <>
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
