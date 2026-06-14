// "First non-fake: catalog entry, else first entry, else fake:ok" -- the
// sensible default model used across the wizard when the customer hasn't
// picked one.
export function pickDefaultModel(entries) {
  if (!entries?.length) return 'fake:ok'
  const preferred = entries.find((entry) => !entry.spec.startsWith('fake:')) ?? entries[0]
  return preferred.spec
}
