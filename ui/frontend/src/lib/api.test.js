import { describe, it, expect, beforeEach, vi } from 'vitest'
import { api } from './api'

describe('request() error handling', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('throws an error carrying the HTTP status on a non-OK response', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      json: async () => ({ detail: 'Platform operators do not belong to an organization' }),
    })

    await expect(api.listWorkflows()).rejects.toMatchObject({
      status: 403,
      message: 'Platform operators do not belong to an organization',
    })
  })

  it('propagates a network failure as an error with no status', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))

    const err = await api.listWorkflows().catch((e) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err.status).toBeUndefined()
  })
})

describe('automationResultsSummary', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('sends the browser UTC offset so the backend can bound by the local day, not UTC (Codex review finding)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) })
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(-600) // UTC+10

    await api.automationResultsSummary('2026-08-03')

    const url = globalThis.fetch.mock.calls[0][0]
    const params = new URL(url).searchParams
    expect(params.get('date')).toBe('2026-08-03')
    expect(params.get('tz_offset_minutes')).toBe('-600')
  })
})
