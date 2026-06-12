import { useCallback, useEffect, useState } from 'react'
import { api } from './api'

// Loads a builder session by id and exposes a `refresh()` to re-fetch after
// any wizard-stage mutation (requirements/specification/solution/deploy).
export function useBuilderSession(sessionId) {
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const refresh = useCallback(() => {
    if (!sessionId) {
      setLoading(false)
      return Promise.resolve(null)
    }
    setLoading(true)
    setError(null)
    return api
      .getSession(sessionId)
      .then((data) => {
        setSession(data)
        return data
      })
      .catch((e) => {
        setError(e.message)
        return null
      })
      .finally(() => setLoading(false))
  }, [sessionId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount/sessionId-change
    refresh()
  }, [refresh])

  return { session, setSession, loading, error, refresh }
}
