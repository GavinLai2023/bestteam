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

  it('prefers the entry an admin marked is_default over alphabetical order', () => {
    const entries: ModelCatalogEntry[] = [
      { spec: 'anthropic:claude', display_name: 'Claude' },
      { spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini', is_default: true },
    ]
    expect(pickDefaultModel(entries)).toBe('openai:gpt-4o-mini')
  })

  it('ignores is_default on a fake: entry', () => {
    const entries: ModelCatalogEntry[] = [
      { spec: 'fake:ok', display_name: 'Demo Assistant', is_default: true },
      { spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini' },
    ]
    expect(pickDefaultModel(entries)).toBe('openai:gpt-4o-mini')
  })
})
