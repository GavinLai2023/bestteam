import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('closes an open transcript when the workflow changes, instead of mislabeling it as the new team', async () => {
    mockedApi.getShareSessionMessages.mockResolvedValue([
      { role: 'user', content: 'hi', turn_number: 1 },
      { role: 'assistant', content: 'hello!', turn_number: 2 },
    ])
    const { rerender } = render(<SharedSessionsPanel workflowId={5} />)
    fireEvent.click(await screen.findByText(/view/i))
    await waitFor(() => expect(screen.getByText('hello!')).toBeInTheDocument())

    mockedApi.listShareLinks.mockResolvedValue([
      { id: 2, workflow_id: 6, token: 'tok2', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.listShareSessions.mockResolvedValue([
      { id: 10, created_at: '2026-08-14T00:00:00+00:00', last_active_at: '2026-08-14T02:00:00+00:00', turns_today: 7 },
    ])
    rerender(<SharedSessionsPanel workflowId={6} />)

    expect(screen.queryByText('hello!')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/7/)).toBeInTheDocument())
  })

  it('clears the old teams links immediately on a workflow change, not just the transcript', async () => {
    // Only `transcript`/`error` used to be reset on a team switch -- the old
    // team's links/sessions kept showing while the new team's request was
    // still in flight, silently mislabeled as belonging to the new team
    // (Codex review finding).
    const { rerender } = render(<SharedSessionsPanel workflowId={5} />)
    await waitFor(() => expect(screen.getByText(/3/)).toBeInTheDocument())

    // The new team's request hangs forever in this test -- the old team's
    // row must already be gone rather than lingering.
    mockedApi.listShareLinks.mockReturnValue(new Promise(() => {}))
    rerender(<SharedSessionsPanel workflowId={6} />)

    await waitFor(() => expect(screen.queryByText(/3/)).not.toBeInTheDocument())
  })

  it('ignores a stale response from a superseded workflow request', async () => {
    // An older, slower request resolving after a newer one must not
    // overwrite the panel with the wrong team's data (Codex review
    // finding).
    let resolveFirst: (value: unknown) => void = () => {}
    const firstRequest = new Promise((resolve) => {
      resolveFirst = resolve
    })
    mockedApi.listShareLinks.mockReturnValueOnce(firstRequest as ReturnType<typeof mockedApi.listShareLinks>)

    const { rerender } = render(<SharedSessionsPanel workflowId={5} />)

    mockedApi.listShareLinks.mockResolvedValueOnce([
      { id: 2, workflow_id: 6, token: 'tok2', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
    ])
    mockedApi.listShareSessions.mockResolvedValue([
      { id: 10, created_at: '2026-08-14T00:00:00+00:00', last_active_at: '2026-08-14T02:00:00+00:00', turns_today: 7 },
    ])
    rerender(<SharedSessionsPanel workflowId={6} />)
    await waitFor(() => expect(screen.getByText(/7/)).toBeInTheDocument())

    // The stale workflowId=5 request finally resolves -- it must not clobber
    // the already-displayed workflowId=6 data.
    await act(async () => {
      resolveFirst([
        { id: 1, workflow_id: 5, token: 'tok', active: true, daily_cap: 30, expires_at: null, created_at: '2026-08-14T00:00:00+00:00' },
      ])
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(screen.getByText(/7/)).toBeInTheDocument()
    expect(screen.queryByText(/3/)).not.toBeInTheDocument()
  })
})
