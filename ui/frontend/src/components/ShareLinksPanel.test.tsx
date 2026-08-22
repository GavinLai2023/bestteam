import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import ShareLinksPanel from './ShareLinksPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listShareLinks: vi.fn(),
    createShareLink: vi.fn(),
    patchShareLink: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('ShareLinksPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listShareLinks.mockResolvedValue([])
  })

  it('lists existing links and shows their status', async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, pipeline_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    await waitFor(() => expect(screen.getByText(/active/i)).toBeInTheDocument())
  })

  it('creates a new link with the chosen daily cap and expiry', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, pipeline_id: 5, token: 'newtoken', active: true, daily_cap: 10, expires_at: '2030-01-02T23:59:59+00:00', created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.change(await screen.findByLabelText(/messages per day/i), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText(/expires on/i), { target: { value: '2030-01-02' } })
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))
    await waitFor(() =>
      expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, {
        daily_cap: 10,
        // End of the chosen day in the browser's own time zone, sent with an offset.
        expires_at: new Date(2030, 0, 2, 23, 59, 59).toISOString(),
      }),
    )
  })

  it('creates a link with the default cap and no expiry when the form is left alone', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, pipeline_id: 5, token: 'newtoken', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.click(await screen.findByRole('button', { name: /generate/i }))
    await waitFor(() => expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, { daily_cap: 30 }))
  })

  it("shows each link's daily cap and expiry", async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, pipeline_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
      { id: 2, pipeline_id: 5, token: 'def456token', active: true, daily_cap: 5, expires_at: '2030-01-02T23:59:59+00:00', created_at: '2026-08-14T00:00:00+00:00' },
    ])
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    expect(await screen.findByText('30 messages per day')).toBeInTheDocument()
    expect(screen.getByText('No expiry')).toBeInTheDocument()
    expect(screen.getByText('5 messages per day')).toBeInTheDocument()
    expect(screen.getByText(/^Expires .*2030/)).toBeInTheDocument()
  })

  it('revokes a link on click', async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, pipeline_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.patchShareLink.mockResolvedValue({
      id: 1, pipeline_id: 5, token: 'abc123token', active: false, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.click(await screen.findByRole('button', { name: /revoke/i }))
    await waitFor(() => expect(mockedApi.patchShareLink).toHaveBeenCalledWith(1, { active: false }))
  })

  it('shows a friendly error when the clipboard write is refused', async () => {
    // navigator.clipboard rejects in any non-secure context (plain HTTP on a
    // real host), which used to surface as an unhandled rejection and no
    // feedback at all.
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, pipeline_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('not allowed')) },
    })

    render(<ShareLinksPanel pipelineId={5} />)
    fireEvent.click(screen.getByRole('button', { name: /share/i }))
    fireEvent.click(await screen.findByRole('button', { name: /copy link/i }))

    expect(await screen.findByText(/copy the link/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /copied/i })).not.toBeInTheDocument()
  })
})
