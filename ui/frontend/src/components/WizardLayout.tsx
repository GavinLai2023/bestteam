import { Outlet, useParams } from 'react-router-dom'
import { useBuilderSession } from '../lib/useBuilderSession'
import WizardProgress from './WizardProgress'
import './WizardLayout.css'

// Shared chrome for all six wizard stages: loads the builder session (if any)
// once, shows the progress bar, and hands `{ session, setSession, refresh,
// sessionId }` down to the active stage page via `useOutletContext()`.
export default function WizardLayout() {
  const { sessionId } = useParams()
  const { session, setSession, loading, error, refresh } = useBuilderSession(sessionId)

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>Build your AI team</h1>
        <p>Answer a few questions and we'll design, test, and launch a custom AI team for you.</p>
      </header>

      <WizardProgress session={session} />

      {error && <p className="banner banner-error">Couldn't load this session: {error}</p>}

      <Outlet context={{ session, setSession, loading, refresh, sessionId }} />
    </div>
  )
}
