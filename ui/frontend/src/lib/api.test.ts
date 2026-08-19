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

    await expect(api.listPipelines()).rejects.toMatchObject({
      status: 403,
      message: 'Platform operators do not belong to an organization',
    })
  })

  it('propagates a network failure as an error with no status', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))

    const err = (await api.listPipelines().catch((e) => e)) as { status?: number; message: string }
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

    await expect(api.listPipelines()).rejects.toMatchObject({
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

describe('share links (org self-service)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('createShareLink posts to the pipeline share-links endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({
        id: 1,
        pipeline_id: 5,
        token: 'tok',
        active: true,
        daily_cap: 30,
        expires_at: null,
        created_at: '2026-08-14T00:00:00+00:00',
      }),
    })
    globalThis.fetch = fetchMock

    const result = await api.createShareLink(5, { daily_cap: 30 })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/pipelines/5/share-links'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ daily_cap: 30 }) }),
    )
    expect(result.token).toBe('tok')
  })

  it('listShareLinks gets the pipeline share-links endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] })
    globalThis.fetch = fetchMock

    await api.listShareLinks(5)

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/pipelines/5/share-links'), expect.anything())
  })

  it('patchShareLink sends a PATCH to the share-link endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        id: 1,
        pipeline_id: 5,
        token: 'tok',
        active: false,
        daily_cap: 30,
        expires_at: null,
        created_at: '2026-08-14T00:00:00+00:00',
      }),
    })
    globalThis.fetch = fetchMock

    const result = await api.patchShareLink(1, { active: false })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/share-links/1'),
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ active: false }) }),
    )
    expect(result.active).toBe(false)
  })

  it('listShareSessions gets the share-link sessions endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] })
    globalThis.fetch = fetchMock

    await api.listShareSessions(1)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/share-links/1/sessions'),
      expect.anything(),
    )
  })

  it('getShareSessionMessages gets the share-link session messages endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] })
    globalThis.fetch = fetchMock

    await api.getShareSessionMessages(1, 9)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/share-links/1/sessions/9/messages'),
      expect.anything(),
    )
  })
})
