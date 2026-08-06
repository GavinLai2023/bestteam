import { useEffect, useState } from 'react'
import { api } from './api'
import type { Me } from './types'

// Fetch the current user's identity/role once. Used by the nav shell and the
// admin route guard to show/hide the admin-only pages. Frontend gating is
// cosmetic -- the backend enforces admin on every /api/config and /api/memory
// call regardless.
export function useMe() {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    api
      .me()
      .then((data) => active && setMe(data))
      .catch(() => active && setMe(null))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  return { me, loading, isAdmin: Boolean(me?.is_admin) }
}
