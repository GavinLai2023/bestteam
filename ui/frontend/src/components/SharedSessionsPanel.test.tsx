import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import SharedSessionsPanel from './SharedSessionsPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listShareLinks: vi.fn(),
    listShareSessions: vi.fn(),
    getShareSessionMessages: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('SharedSessionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listShareLinks.mockResolvedValue([
      { id: 1, workflow_id: 5, token: 'tok', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.listShareSessions.mockResolvedValue([
      { id: 9, created_at: '2026-08-14T00:00:00+00:00', last_active_at: '2026-08-14T01:00:00+00:00', turns_today: 3 },
    ])
  })

  it('lists sessions for a share link', async () => {
    render(<SharedSessionsPanel workflowId={5} />)
    await waitFor(() => expect(screen.getByText(/3/)).toBeInTheDocument())
  })

  it('shows a session transcript on click', async () => {
    mockedApi.getShareSessionMessages.mockResolvedValue([
      { role: 'user', content: 'hi', turn_number: 1 },
      { role: 'assistant', content: 'hello!', turn_number: 2 },
    ])
    render(<SharedSessionsPanel workflowId={5} />)
    fireEvent.click(await screen.findByText(/view/i))
    await waitFor(() => expect(screen.getByText('hello!')).toBeInTheDocument())
  })
})
