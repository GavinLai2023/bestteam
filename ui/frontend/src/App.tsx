import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import WizardLayout from './components/WizardLayout'
import LoginPage from './pages/LoginPage'
import LandingPage from './pages/LandingPage'
import MonitorPage from './pages/MonitorPage'
import ActivityPage from './pages/ActivityPage'
import AdvancedPage from './pages/AdvancedPage'
import MemoryPage from './pages/MemoryPage'
import TracePage from './pages/TracePage'
import AccountsPage from './pages/AccountsPage'
import IntentPage from './pages/wizard/IntentPage'
import DocumentsPage from './pages/wizard/DocumentsPage'
import PreviewPage from './pages/wizard/PreviewPage'
import ConfirmPage from './pages/wizard/ConfirmPage'
import DeployPage from './pages/wizard/DeployPage'
import SessionsPage from './pages/wizard/SessionsPage'
import ShareChatPage from './pages/ShareChatPage'
import { useMe } from './lib/useMe'

function RequireAuth() {
  return localStorage.getItem('bestteam_token') ? <Outlet /> : <Navigate to="/login" replace />
}

// Cosmetic gate for admin-only pages; the backend enforces admin on every
// /api/config and /api/memory call regardless. Non-admins are sent home.
export function RequireAdmin() {
  const { loading, isAdmin } = useMe()
  if (loading) return null
  return isAdmin ? <Outlet /> : <Navigate to="/" replace />
}

// Mirror of RequireAdmin for the customer-facing pages. A platform operator
// (is_admin, org_id NULL) has no org, so every org-scoped surface 403s for
// them -- send them to their admin home instead of a dead-end customer page.
// Cosmetic, like RequireAdmin: the backend is the real authority.
export function RequireOrgMember() {
  const { loading, isAdmin } = useMe()
  if (loading) return null
  return isAdmin ? <Navigate to="/advanced" replace /> : <Outlet />
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/share/:token" element={<ShareChatPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route element={<RequireOrgMember />}>
            {/* The landing route: sends a brand-new org (no deployed
                pipelines yet) to the wizard, and everyone else to the
                Activity dashboard -- see LandingPage.tsx. "Run a team" is
                a deliberate destination now (/run), not the default one. */}
            <Route path="/" element={<LandingPage />} />
            <Route path="/run" element={<MonitorPage />} />
            <Route path="/teams" element={<SessionsPage />} />
            <Route path="/activity" element={<ActivityPage />} />

            <Route path="/wizard" element={<WizardLayout />}>
              <Route index element={<IntentPage />} />
              <Route path=":sessionId/documents" element={<DocumentsPage />} />
              <Route path=":sessionId/preview" element={<PreviewPage />} />
              <Route path=":sessionId/confirm" element={<ConfirmPage />} />
              <Route path=":sessionId/deploy" element={<DeployPage />} />
            </Route>
          </Route>

          <Route element={<RequireAdmin />}>
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/advanced" element={<AdvancedPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/trace" element={<TracePage />} />
          </Route>

          {/* Kept outside both guards so an unknown path routes to `/`, where
              RequireOrgMember/LandingPage decide the destination
              (operator -> /advanced; org member -> /wizard or /activity). */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  )
}

export default App
