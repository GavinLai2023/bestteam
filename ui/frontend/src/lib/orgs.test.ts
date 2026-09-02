import { describe, expect, it } from 'vitest'
import { visibleOrgOptions } from './orgs'
import type { AdminOrg } from './types'

const orgs: AdminOrg[] = [
  { name: 'a', active: true },
  { name: 'b', active: false },
]

describe('visibleOrgOptions', () => {
  it('hides inactive orgs by default', () => {
    expect(visibleOrgOptions(orgs, false).map((o) => o.name)).toEqual(['a'])
  })

  it('shows everything when asked', () => {
    expect(visibleOrgOptions(orgs, true).map((o) => o.name)).toEqual(['a', 'b'])
  })

  it('keeps a selected inactive org visible so the selection cannot vanish', () => {
    expect(visibleOrgOptions(orgs, false, 'b').map((o) => o.name)).toEqual(['a', 'b'])
  })
})
