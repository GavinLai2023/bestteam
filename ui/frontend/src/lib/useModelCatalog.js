import { useEffect, useState } from 'react'
import { api } from './api'

// Fetches `/api/config/model-catalog` once. Used by wizard stages that let
// the customer (or default to) a model the Solution Architect should use.
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
