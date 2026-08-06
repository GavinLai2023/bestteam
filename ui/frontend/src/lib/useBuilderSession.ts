import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { BuilderSession } from './types'

// Loads a builder session by id and exposes a `refresh()` to re-fetch after
// any wizard-stage mutation (requirements/specification/solution/deploy).
export function useBuilderSession(sessionId: string | undefined) {
  const [session, setSession] = useState<BuilderSession | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback((): Promise<BuilderSession | null> => {
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
      .catch((e: Error) => {
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
