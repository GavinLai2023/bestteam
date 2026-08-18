import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import KnowledgeBasesPanel from './KnowledgeBasesPanel'
import { api } from '../lib/api'
import type { OrgKnowledgeBase } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    listOwnKnowledgeBases: vi.fn(),
    deleteOwnKnowledgeBase: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const kb = (overrides: Partial<OrgKnowledgeBase> = {}): OrgKnowledgeBase => ({
  name: 'policies',
  description: null,
  type: 'local_folder',
  updated_at: '2026-08-18T00:00:00Z',
  used_by: [],
  servable: true,
  latest_job: {
    job_id: 1,
    status: 'completed',
    file_count: 3,
    documents_succeeded: 3,
    documents_failed: 0,
    chunk_count: 12,
    errors: [],
  },
  ...overrides,
})

describe('KnowledgeBasesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb()])
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders nothing at all when the org has no documents', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([])
    const { container } = render(<KnowledgeBasesPanel />)
    await waitFor(() => expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('lists a ready collection with its document count', async () => {
    render(<KnowledgeBasesPanel />)
    expect(await screen.findByText('policies')).toBeInTheDocument()
    expect(screen.getByText(/Ready · 3 documents/)).toBeInTheDocument()
  })

  it('says a collection has never been indexed when it has no job', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ latest_job: null })])
    render(<KnowledgeBasesPanel />)
    expect(await screen.findByText(/Not indexed yet/)).toBeInTheDocument()
  })

  it('lets the reader expand the files a completed upload skipped', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        latest_job: {
          job_id: 1, status: 'completed', file_count: 2, documents_succeeded: 1,
          documents_failed: 1, chunk_count: 4,
          errors: [{ filename: 'scan.pdf', error: 'no text layer' }],
        },
      }),
    ])
    render(<KnowledgeBasesPanel />)

    const toggle = await screen.findByRole('button', { name: /1 file skipped/i })
    expect(screen.queryByText(/no text layer/)).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(toggle)
    })
    expect(screen.getByText(/scan\.pdf/)).toBeInTheDocument()
    expect(screen.getByText(/no text layer/)).toBeInTheDocument()
  })

  it("shows a failed upload's own error, and says the previous version is still live", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        servable: true,
        latest_job: {
          job_id: 2, status: 'failed', file_count: 1, documents_succeeded: 0,
          documents_failed: 1, chunk_count: 0,
          errors: [{ filename: 'blank.txt', error: 'document produced no chunks' }],
        },
      }),
    ])
    render(<KnowledgeBasesPanel />)

    expect(await screen.findByText(/document produced no chunks/)).toBeInTheDocument()
    expect(screen.getByText(/an earlier version is still in use/)).toBeInTheDocument()
  })

  it('does not claim an earlier version is live when nothing ever succeeded', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        servable: false,
        latest_job: {
          job_id: 2, status: 'failed', file_count: 1, documents_succeeded: 0,
          documents_failed: 1, chunk_count: 0,
          errors: [{ filename: null, error: 'Knowledge base has no readable documents' }],
        },
      }),
    ])
    render(<KnowledgeBasesPanel />)

    expect(await screen.findByText(/no readable documents/)).toBeInTheDocument()
    expect(screen.queryByText(/an earlier version is still in use/)).not.toBeInTheDocument()
  })

  it('names the teams using a collection', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ used_by: ['support_team', 'sales_team'] })])
    render(<KnowledgeBasesPanel />)
    expect(await screen.findByText(/support_team, sales_team/)).toBeInTheDocument()
  })

  it('disables Delete while an upload is still processing, and explains why', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        latest_job: {
          job_id: 3, status: 'running', file_count: 1, documents_succeeded: 0,
          documents_failed: 0, chunk_count: 0, errors: [],
        },
      }),
    ])
    vi.useFakeTimers()
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    const button = screen.getByRole('button', { name: /delete/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringMatching(/still processing/i))
  })

  it('disables Delete while a live team uses the collection, and explains why', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ used_by: ['support_team'] })])
    render(<KnowledgeBasesPanel />)

    const button = await screen.findByRole('button', { name: /delete/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringMatching(/support_team/))
  })

  it('does nothing if the reader cancels the confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    expect(mockedApi.deleteOwnKnowledgeBase).not.toHaveBeenCalled()
    expect(screen.getByText('policies')).toBeInTheDocument()
  })

  it('deletes the collection and drops its row once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockedApi.deleteOwnKnowledgeBase.mockResolvedValue(undefined)
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    expect(mockedApi.deleteOwnKnowledgeBase).toHaveBeenCalledWith('policies')
    await waitFor(() => expect(screen.queryByText('policies')).not.toBeInTheDocument())
  })

  it("shows a refused delete's message on the row it belongs to", async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockedApi.deleteOwnKnowledgeBase.mockRejectedValue(
      new Error("Can't delete 'policies': it's used by deployed team(s): support_team."),
    )
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    expect(await screen.findByText(/used by deployed team\(s\): support_team/)).toBeInTheDocument()
    expect(screen.getByText('policies')).toBeInTheDocument()
  })

  it('polls every 3s while an upload is processing, and stops once it finishes', async () => {
    const processing = kb({
      latest_job: {
        job_id: 3, status: 'queued', file_count: 1, documents_succeeded: 0,
        documents_failed: 0, chunk_count: 0, errors: [],
      },
    })
    mockedApi.listOwnKnowledgeBases
      .mockResolvedValueOnce([processing])
      .mockResolvedValue([kb()])

    vi.useFakeTimers()
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/Processing/)).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2)
    expect(screen.getByText(/Ready · 3 documents/)).toBeInTheDocument()

    // Nothing is processing any more, so the poll must stop -- otherwise an
    // idle "My teams" page hits this endpoint forever.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000)
    })
    expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2)
  })

  it('does not poll at all when nothing is processing', async () => {
    vi.useFakeTimers()
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(1)
  })

  it('re-fetches on demand from the Refresh button', async () => {
    render(<KnowledgeBasesPanel />)

    const refreshButton = await screen.findByRole('button', { name: /refresh/i })
    await act(async () => {
      fireEvent.click(refreshButton)
    })
    expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2)
  })

  it('shows a banner when the list itself cannot be loaded', async () => {
    mockedApi.listOwnKnowledgeBases.mockRejectedValue(new Error('Not authenticated'))
    render(<KnowledgeBasesPanel />)
    expect(await screen.findByText('Not authenticated')).toBeInTheDocument()
  })
})
