import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { api } from '../lib/api'

// The "/" route for an org member: a brand-new org with nothing deployed
// yet has no use for the Activity dashboard (it'd be empty), so send it to
// the wizard instead. Everyone else lands on Activity, not Run a Team --
// that's a deliberate destination (/run) now, not the daily home.
export default function LandingPage() {
  const [destination, setDestination] = useState<string | null>(null)

  useEffect(() => {
    api
      .listWorkflows()
      .then((data) => setDestination(data.workflows.length ? '/activity' : '/wizard'))
      .catch(() => setDestination('/activity'))
  }, [])

  if (!destination) return null
  return <Navigate to={destination} replace />
}
