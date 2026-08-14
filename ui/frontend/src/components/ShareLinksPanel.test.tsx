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
      { id: 1, workflow_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    render(<ShareLinksPanel workflowId={5} />)
    await waitFor(() => expect(screen.getByText(/active/i)).toBeInTheDocument())
  })

  it('creates a new link on click', async () => {
    mockedApi.createShareLink.mockResolvedValue({
      id: 2, workflow_id: 5, token: 'newtoken', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel workflowId={5} />)
    fireEvent.click(await screen.findByRole('button', { name: /generate/i }))
    await waitFor(() => expect(mockedApi.createShareLink).toHaveBeenCalledWith(5, expect.any(Object)))
  })

  it('revokes a link on click', async () => {
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, workflow_id: 5, token: 'abc123token', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.patchShareLink.mockResolvedValue({
      id: 1, workflow_id: 5, token: 'abc123token', active: false, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00',
    })
    render(<ShareLinksPanel workflowId={5} />)
    fireEvent.click(await screen.findByRole('button', { name: /revoke/i }))
    await waitFor(() => expect(mockedApi.patchShareLink).toHaveBeenCalledWith(1, { active: false }))
  })
})
