import { describe, expect, it } from 'vitest'
import { pickDefaultModel } from './models'
import type { ModelCatalogEntry } from './types'

describe('pickDefaultModel', () => {
  it('prefers a real model over a fake-architect: entry', () => {
    const entries: ModelCatalogEntry[] = [
      { spec: 'fake-architect:e2e', display_name: 'E2E Test Architect (fake, $0)' },
      { spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini' },
    ]
    expect(pickDefaultModel(entries)).toBe('openai:gpt-4o-mini')
  })
})
