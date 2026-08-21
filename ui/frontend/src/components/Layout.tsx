import { useEffect } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMe } from '../lib/useMe'
import { SUPPORTED_LANGUAGES, setLanguage, type LanguageCode } from '../lib/i18n'
import './Layout.css'

export default function Layout() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { isAdmin } = useMe()
  const { t, i18n } = useTranslation()

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
          <svg className="brand-mark" width="22" height="22" viewBox="0 0 26 26" fill="none" aria-hidden="true">
            <rect x="2" y="2" width="14" height="22" rx="7" className="brand-mark-soft" />
            <rect x="9" y="2" width="15" height="14" rx="7" className="brand-mark-strong" />
          </svg>
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
          <select
            className="language-select"
            aria-label={t('nav.language')}
            value={i18n.resolvedLanguage}
            onChange={(e) => setLanguage(e.target.value as LanguageCode)}
          >
            {SUPPORTED_LANGUAGES.map((lang) => (
              <option key={lang.code} value={lang.code}>
                {lang.label}
              </option>
            ))}
          </select>
          <button type="button" className="logout-button" onClick={logOut}>
            {t('nav.logOut')}
          </button>
        </div>
      </nav>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  )
}
