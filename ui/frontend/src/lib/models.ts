import type { ModelCatalogEntry } from './types'

// "First non-fake: catalog entry, else first entry, else fake:ok" -- the
// sensible default model used across the wizard when the customer hasn't
// picked one. "fake-architect:" is excluded from the preferred group too:
// it's a deterministic stub reserved for automated E2E coverage (see the
// root CLAUDE.md), and "fake:".startsWith("fake:") alone would miss it.
export function pickDefaultModel(entries: ModelCatalogEntry[] | undefined): string {
  if (!entries?.length) return 'fake:ok'
  const isFake = (spec: string) => spec.startsWith('fake:') || spec.startsWith('fake-architect:')
  const preferred = entries.find((entry) => !isFake(entry.spec)) ?? entries[0]
  return preferred.spec
}
