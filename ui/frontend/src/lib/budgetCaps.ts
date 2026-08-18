// Reading a budget-cap input box. Its own module, like `dateFormat.ts`,
// because the failure it guards against cannot be reached through the
// component: `type="number"` sanitises junk to '' (verified in jsdom and true
// of real browsers), so the only way to hold this behaviour still is to test
// the function directly -- and `react-refresh/only-export-components` makes
// exporting it from EmailBudgetSettings.tsx a lint error.

// Three distinct answers, none of which may be folded into another:
//
//   null       an empty box -- no cap. NOT 0, which is a real and different
//              setting: a cap of zero, i.e. automation switched off.
//   undefined  not a number at all. Also not null, because `Number('abc')` is
//              NaN and `JSON.stringify` turns NaN into null -- so a figure the
//              browser ever let through would arrive at the API as "remove
//              this cap" and a customer's spend limit would disappear with
//              nothing said. The caller refuses to save instead.
//   number     the cap. Out-of-range values are the API's to reject, not
//              ours: it has the authoritative bounds and a message to match.
export function parseCap(raw: string): number | null | undefined {
  const trimmed = raw.trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isFinite(value) ? value : undefined
}
