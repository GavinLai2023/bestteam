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
  // `!== false` on purpose: a payload without the flag must not hide the org.
  return orgs.filter((o) => o.active !== false || o.name === selected)
}
