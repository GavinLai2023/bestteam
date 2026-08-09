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

    const err = (await api.listWorkflows().catch((e) => e)) as { status?: number; message: string }
    expect(err).toBeInstanceOf(Error)
    expect(err.status).toBeUndefined()
  })

  it('formats a FastAPI validation-error array into a readable message instead of dumping raw JSON', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      json: async () => ({
        detail: [
          {
            type: 'value_error',
            loc: ['body', 'name'],
            msg: "Value error, identifier may contain only letters, digits, '.', '_' and '-'",
            input: 'Property Management INC',
            ctx: { error: {} },
          },
        ],
      }),
    })

    await expect(api.listWorkflows()).rejects.toMatchObject({
      status: 422,
      message: "name: identifier may contain only letters, digits, '.', '_' and '-'",
    })
  })
})

describe('automationResultsSummary', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('sends the browser UTC offset so the backend can bound by the local day, not UTC (Codex review finding)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) })
    globalThis.fetch = fetchMock
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(-600) // UTC+10

    await api.automationResultsSummary('2026-08-03')

    const url = fetchMock.mock.calls[0][0] as string
    const params = new URL(url).searchParams
    expect(params.get('date')).toBe('2026-08-03')
    expect(params.get('tz_offset_minutes')).toBe('-600')
  })
})
