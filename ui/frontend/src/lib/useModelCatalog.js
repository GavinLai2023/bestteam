import { useEffect, useState } from 'react'
import { api } from './api'

// Fetches `/api/model-catalog` once (the non-admin read endpoint -- the wizard
// runs as an org member). Used by wizard stages that let the customer pick (or
// default to) a model the Solution Architect should use.
export function useModelCatalog() {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .modelCatalog()
      .then(setEntries)
      .catch(() => setEntries([]))
      .finally(() => setLoading(false))
  }, [])

  return { entries, loading }
}
