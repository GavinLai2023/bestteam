import { NavLink, Outlet } from 'react-router-dom'
import './Layout.css'

export default function Layout() {
  return (
    <div className="app-shell">
      <nav className="top-nav">
        <span className="brand">bestteam</span>
        <div className="top-nav-links">
          <NavLink to="/wizard" className={({ isActive }) => (isActive ? 'active' : '')}>
            Build a team
          </NavLink>
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
            Talk to your team
          </NavLink>
          <NavLink to="/advanced" className={({ isActive }) => (isActive ? 'active' : '')}>
            Advanced
          </NavLink>
        </div>
      </nav>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  )
}
