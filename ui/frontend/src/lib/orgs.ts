import type { AdminOrg } from './types'

// Admin org dropdowns default to active organisations; a deactivated org that
// is already selected stays listed so the selection can't silently vanish
// (AccountsPage is untouched -- it is where deactivated orgs are managed and
// must always show them).
export function visibleOrgOptions(
  orgs: AdminOrg[],
  showInactive: boolean,
  selected?: string | null,
): AdminOrg[] {
  if (showInactive) return orgs
  return orgs.filter((o) => o.active || o.name === selected)
}
