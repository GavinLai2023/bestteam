import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMe } from '../lib/useMe'
import BrandMark from './BrandMark'
import ChangePasswordDialog from './ChangePasswordDialog'
import LanguageSelect from './LanguageSelect'
import './Layout.css'

export default function Layout() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { isAdmin } = useMe()
  const { t } = useTranslation()
  const [changingPassword, setChangingPassword] = useState(false)

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
        <span className="brand">
          <BrandMark />
          {t('nav.brand')}
        </span>
        <div className="top-nav-links">
          {/* Customer pages are org-scoped; a platform operator (no org) can't
              use them, so show them only to org members. */}
          {!isAdmin && (
            <>
              <NavLink to="/activity" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.dashboard')}
              </NavLink>
              <NavLink to="/wizard" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.buildTeam')}
              </NavLink>
              <NavLink to="/teams" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.myTeams')}
              </NavLink>
              <NavLink to="/run" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.runTeam')}
              </NavLink>
            </>
          )}
          {isAdmin && (
            <>
              <NavLink to="/accounts" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.accounts')}
              </NavLink>
              <NavLink to="/advanced" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.advanced')}
              </NavLink>
              <NavLink to="/memory" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.memory')}
              </NavLink>
              <NavLink to="/trace" className={({ isActive }) => (isActive ? 'active' : '')}>
                {t('nav.trace')}
              </NavLink>
            </>
          )}
          <LanguageSelect />
          {/* Shown to platform operators too: an operator's own password
              deserves the same self-service as a customer's. */}
          <button
            type="button"
            className="nav-action"
            onClick={() => setChangingPassword(true)}
          >
            {t('nav.changePassword')}
          </button>
          {/* `logout-button` carries no styling any more -- it is kept because
              tests/e2e/test_smoke.py clicks `button.logout-button`, and it must
              stay unique to this button for Playwright's strict mode. */}
          <button type="button" className="nav-action logout-button" onClick={logOut}>
            {t('nav.logOut')}
          </button>
        </div>
      </nav>
      <main className="app-main">
        <Outlet />
      </main>
      <ChangePasswordDialog open={changingPassword} onClose={() => setChangingPassword(false)} />
    </div>
  )
}
