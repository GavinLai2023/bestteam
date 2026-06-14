import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import WizardLayout from './components/WizardLayout'
import MonitorPage from './pages/MonitorPage'
import AdvancedPage from './pages/AdvancedPage'
import IntentPage from './pages/wizard/IntentPage'
import PreviewPage from './pages/wizard/PreviewPage'
import ConfirmPage from './pages/wizard/ConfirmPage'
import DeployPage from './pages/wizard/DeployPage'

function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<MonitorPage />} />
        <Route path="/advanced" element={<AdvancedPage />} />

        <Route path="/wizard" element={<WizardLayout />}>
          <Route index element={<IntentPage />} />
          <Route path=":sessionId/preview" element={<PreviewPage />} />
          <Route path=":sessionId/confirm" element={<ConfirmPage />} />
          <Route path=":sessionId/deploy" element={<DeployPage />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export default App
