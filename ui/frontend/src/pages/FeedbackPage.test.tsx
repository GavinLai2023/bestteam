import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import FeedbackPage from './FeedbackPage'
import { api } from '../lib/api'
import type { FeedbackItem } from '../lib/types'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, api: { adminFeedback: vi.fn(), patchFeedback: vi.fn() } }
})

const mockedApi = vi.mocked(api)

const rows: FeedbackItem[] = [
  {
    id: 1,
    kind: 'defect',
    body: 'The trace page shows <b>nothing</b> for my last run',
    status: 'new',
    admin_note: null,
    org_name: 'acme',
    username: 'alice',
    source: 'user',
    context: { page: '/trace', locale: 'en' },
    created_at: '2026-08-26T02:00:00+00:00',
  },
  {
    id: 2,
    kind: 'suggestion',
    body: 'let the team answer in French',
    status: 'acknowledged',
    admin_note: 'on the list',
    org_name: 'acme',
    username: null,
    source: 'visitor',
    context: { share_link_id: 7 },
    created_at: '2026-08-26T01:00:00+00:00',
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.adminFeedback.mockResolvedValue({ feedback: rows })
  mockedApi.patchFeedback.mockResolvedValue({ ok: true })
})

describe('FeedbackPage', () => {
  it('lists feedback with source and org', async () => {
    render(<FeedbackPage />)
    expect(await screen.findByText(/alice/)).toBeInTheDocument()
    expect(screen.getAllByText(/visitor/i).length).toBeGreaterThan(1) // hint + the visitor row
    expect(screen.getAllByText(/acme/).length).toBe(2)
  })

  it('refetches when the status filter changes', async () => {
    render(<FeedbackPage />)
    await screen.findByText(/alice/)
    fireEvent.change(screen.getByLabelText(/status/i), { target: { value: 'new' } })
    await waitFor(() =>
      expect(mockedApi.adminFeedback).toHaveBeenLastCalledWith({ status: 'new', kind: '' }),
    )
  })

  it('expands a row to the full body as plain text', async () => {
    render(<FeedbackPage />)
    fireEvent.click(await screen.findByText(/The trace page shows/))
    // The markup in the body must render as literal text, never as HTML.
    const matches = screen.getAllByText('The trace page shows <b>nothing</b> for my last run')
    expect(matches.some((el) => el.tagName === 'PRE')).toBe(true)
    expect(screen.getByText(/\/trace/)).toBeInTheDocument()
  })

  it('saves a status change and a note', async () => {
    render(<FeedbackPage />)
    fireEvent.click(await screen.findByText(/The trace page shows/))
    fireEvent.change(screen.getByLabelText(/set status/i), { target: { value: 'resolved' } })
    fireEvent.change(screen.getByLabelText(/note/i), { target: { value: 'fixed in #96' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    await waitFor(() =>
      expect(mockedApi.patchFeedback).toHaveBeenCalledWith(1, {
        status: 'resolved',
        admin_note: 'fixed in #96',
      }),
    )
  })

  it('shows the error when loading fails', async () => {
    mockedApi.adminFeedback.mockRejectedValue(new Error('boom'))
    render(<FeedbackPage />)
    expect(await screen.findByText('boom')).toBeInTheDocument()
  })
})
