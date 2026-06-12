import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import WizardLayout from './components/WizardLayout'
import MonitorPage from './pages/MonitorPage'
import AdvancedPage from './pages/AdvancedPage'
import IntentPage from './pages/wizard/IntentPage'
import RequirementsPage from './pages/wizard/RequirementsPage'
import TeamPage from './pages/wizard/TeamPage'
import RefinePage from './pages/wizard/RefinePage'
import TestPage from './pages/wizard/TestPage'
import DeployPage from './pages/wizard/DeployPage'

function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<MonitorPage />} />
        <Route path="/advanced" element={<AdvancedPage />} />

        <Route path="/wizard" element={<WizardLayout />}>
          <Route index element={<IntentPage />} />
          <Route path=":sessionId/requirements" element={<RequirementsPage />} />
          <Route path=":sessionId/team" element={<TeamPage />} />
          <Route path=":sessionId/refine" element={<RefinePage />} />
          <Route path=":sessionId/test" element={<TestPage />} />
          <Route path=":sessionId/deploy" element={<DeployPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export default App
