import { useCallback, useEffect, useState } from 'react'
import { api } from './api'

// Fetches `/api/model-catalog` (the non-admin read endpoint -- the wizard
// runs as an org member). Used by wizard stages that let the customer pick
// (or default to) a model the Solution Architect should use.
// `failed`/`retry` let a caller that silently picks a default model (via
// `pickDefaultModel`) tell "still loading" and "fetch failed" apart from "the
// catalog is genuinely empty" -- the first two must never fall through to a
// `fake:` default in production, only the third legitimately can.
export function useModelCatalog() {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    api
      .modelCatalog()
      .then(setEntries)
      .catch(() => {
        setEntries([])
        setFailed(true)
      })
      .finally(() => setLoading(false))
  }, [attempt])

  const retry = useCallback(() => {
    setLoading(true)
    setFailed(false)
    setAttempt((n) => n + 1)
  }, [])

  return { entries, loading, failed, retry }
}
