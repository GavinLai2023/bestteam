import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import WizardLayout from './components/WizardLayout'
import LoginPage from './pages/LoginPage'
import MonitorPage from './pages/MonitorPage'
import AdvancedPage from './pages/AdvancedPage'
import MemoryPage from './pages/MemoryPage'
import IntentPage from './pages/wizard/IntentPage'
import PreviewPage from './pages/wizard/PreviewPage'
import ConfirmPage from './pages/wizard/ConfirmPage'
import DeployPage from './pages/wizard/DeployPage'
import SessionsPage from './pages/wizard/SessionsPage'
import { useMe } from './lib/useMe'

function RequireAuth() {
  return localStorage.getItem('bestteam_token') ? <Outlet /> : <Navigate to="/login" replace />
}

// Cosmetic gate for admin-only pages; the backend enforces admin on every
// /api/config and /api/memory call regardless. Non-admins are sent home.
function RequireAdmin() {
  const { loading, isAdmin } = useMe()
  if (loading) return null
  return isAdmin ? <Outlet /> : <Navigate to="/" replace />
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/" element={<MonitorPage />} />
          <Route path="/teams" element={<SessionsPage />} />

          <Route element={<RequireAdmin />}>
            <Route path="/advanced" element={<AdvancedPage />} />
            <Route path="/memory" element={<MemoryPage />} />
          </Route>

          <Route path="/wizard" element={<WizardLayout />}>
            <Route index element={<IntentPage />} />
            <Route path=":sessionId/preview" element={<PreviewPage />} />
            <Route path=":sessionId/confirm" element={<ConfirmPage />} />
            <Route path=":sessionId/deploy" element={<DeployPage />} />
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  )
}

export default App
