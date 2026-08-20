import type { ModelCatalogEntry } from './types'

// "The admin's is_default pick, else the first non-fake: catalog entry, else
// first entry, else fake:ok" -- the sensible default model used across the
// wizard for a call the customer never sees or chooses (e.g. the Solution
// Architect's own model). "fake-architect:" is excluded from both the
// is_default and non-fake groups: it's a deterministic stub reserved for
// automated E2E coverage (see the root CLAUDE.md), and
// "fake:".startsWith("fake:") alone would miss it.
export function pickDefaultModel(entries: ModelCatalogEntry[] | undefined): string {
  if (!entries?.length) return 'fake:ok'
  const isFake = (spec: string) => spec.startsWith('fake:') || spec.startsWith('fake-architect:')
  const preferred =
    entries.find((entry) => entry.is_default && !isFake(entry.spec)) ??
    entries.find((entry) => !isFake(entry.spec)) ??
    entries[0]
  return preferred.spec
}
