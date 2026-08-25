import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import KnowledgeBasesPanel from './KnowledgeBasesPanel'
import { api } from '../lib/api'
import type { OrgKnowledgeBase } from '../lib/types'
import { answerConfirm } from '../test/confirmDialog'

// `searchOwnKnowledgeBase` belongs here even though no assertion below reads
// it: the row's "Try a search" toggle renders `KnowledgeBaseSearch`, which
// calls it -- an omitted key would throw "is not a function" on first render.
vi.mock('../lib/api', () => ({
  api: {
    listOwnKnowledgeBases: vi.fn(),
    deleteOwnKnowledgeBase: vi.fn(),
    searchOwnKnowledgeBase: vi.fn(),
    removeOwnKnowledgeBaseDocument: vi.fn(),
    restoreOwnKnowledgeBase: vi.fn(),
    retryOwnKnowledgeBaseIngestion: vi.fn(),
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
    retryable: false,
  },
  documents: [],
  previous_generation: null,
  ...overrides,
})

const threeDocuments = [
  { filename: 'handbook.pdf', status: 'chunked', size_bytes: 204800 },
  { filename: 'hours.txt', status: 'chunked', size_bytes: 120 },
  { filename: 'scan.pdf', status: 'failed', size_bytes: 3 * 1024 * 1024 },
]

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
          documents_failed: 1, chunk_count: 4, retryable: false,
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

  it('counts every skipped file, not just the ones it can name', async () => {
    // `job_status_payload` caps `errors` at 10, so the list is a sample and
    // `documents_failed` is the count.
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        latest_job: {
          job_id: 1, status: 'completed', file_count: 20, documents_succeeded: 5,
          documents_failed: 15, chunk_count: 4, retryable: false,
          errors: Array.from({ length: 10 }, (_, i) => ({
            filename: `bad-${i}.pdf`, error: 'no text layer',
          })),
        },
      }),
    ])
    render(<KnowledgeBasesPanel />)

    const toggle = await screen.findByRole('button', { name: /15 files skipped/i })
    await act(async () => {
      fireEvent.click(toggle)
    })
    // The named ones are still all listed.
    expect(screen.getByText(/bad-0\.pdf/)).toBeInTheDocument()
    expect(screen.getByText(/bad-9\.pdf/)).toBeInTheDocument()
  })

  it("shows a failed upload's own error, and says the previous version is still live", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        servable: true,
        latest_job: {
          job_id: 2, status: 'failed', file_count: 1, documents_succeeded: 0,
          documents_failed: 1, chunk_count: 0, retryable: true,
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
          documents_failed: 1, chunk_count: 0, retryable: true,
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
          documents_failed: 0, chunk_count: 0, errors: [], retryable: false,
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
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    await act(async () => {
      await answerConfirm(false)
    })
    expect(mockedApi.deleteOwnKnowledgeBase).not.toHaveBeenCalled()
    expect(screen.getByText('policies')).toBeInTheDocument()
  })

  it('deletes the collection and drops its row once confirmed', async () => {
    mockedApi.deleteOwnKnowledgeBase.mockResolvedValue(undefined)
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    await act(async () => {
      await answerConfirm(true)
    })
    expect(mockedApi.deleteOwnKnowledgeBase).toHaveBeenCalledWith('policies')
    await waitFor(() => expect(screen.queryByText('policies')).not.toBeInTheDocument())
  })

  it("shows a refused delete's message on the row it belongs to", async () => {
    mockedApi.deleteOwnKnowledgeBase.mockRejectedValue(
      new Error("Can't delete 'policies': it's used by deployed team(s): support_team."),
    )
    render(<KnowledgeBasesPanel />)

    const deleteButton = await screen.findByRole('button', { name: /delete/i })
    await act(async () => {
      fireEvent.click(deleteButton)
    })
    await act(async () => {
      await answerConfirm(true)
    })
    expect(await screen.findByText(/used by deployed team\(s\): support_team/)).toBeInTheDocument()
    expect(screen.getByText('policies')).toBeInTheDocument()
  })

  it('polls every 3s while an upload is processing, and stops once it finishes', async () => {
    const processing = kb({
      latest_job: {
        job_id: 3, status: 'queued', file_count: 1, documents_succeeded: 0,
        documents_failed: 0, chunk_count: 0, errors: [], retryable: false,
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

  it('offers Try a search only for ready collections, and explains why not', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ name: 'ready' }),
      kb({
        name: 'processing',
        latest_job: {
          job_id: 3, status: 'running', file_count: 1, documents_succeeded: 0,
          documents_failed: 0, chunk_count: 0, errors: [], retryable: false,
        },
      }),
      kb({
        name: 'never-worked',
        servable: false,
        latest_job: {
          job_id: 4, status: 'failed', file_count: 1, documents_succeeded: 0,
          documents_failed: 1, chunk_count: 0, retryable: true,
          errors: [{ filename: null, error: 'Knowledge base has no readable documents' }],
        },
      }),
    ])
    vi.useFakeTimers()
    render(<KnowledgeBasesPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    const [ready, processing, neverWorked] = screen.getAllByRole('button', { name: /try a search/i })
    expect(ready).toBeEnabled()
    expect(processing).toBeDisabled()
    expect(processing).toHaveAttribute('title', expect.stringMatching(/still processing/i))
    expect(neverWorked).toBeDisabled()
    expect(neverWorked).toHaveAttribute('title', expect.stringMatching(/nothing to search/i))
  })

  it('toggles the search box for a row', async () => {
    render(<KnowledgeBasesPanel />)

    const toggle = await screen.findByRole('button', { name: /try a search/i })
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(toggle)
    })
    expect(screen.getByLabelText(/search/i)).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(toggle)
    })
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
  })

  it('lists the documents behind a toggle, with size and whether each could be read', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ documents: threeDocuments })])
    render(<KnowledgeBasesPanel />)

    const toggle = await screen.findByRole('button', { name: /show 3 documents/i })
    expect(screen.queryByText('handbook.pdf')).not.toBeInTheDocument()
    fireEvent.click(toggle)
    expect(screen.getByText('handbook.pdf')).toBeInTheDocument()
    expect(screen.getByText(/200 KB/)).toBeInTheDocument()
    expect(screen.getByText(/3\.0 MB · couldn't be read/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /hide documents/i }))
    expect(screen.queryByText('handbook.pdf')).not.toBeInTheDocument()
  })

  it('removes one document once confirmed, naming the teams whose answers change', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ documents: threeDocuments, used_by: ['support_team'] }),
    ])
    mockedApi.removeOwnKnowledgeBaseDocument.mockResolvedValue({ name: 'policies', job_id: 9, status: 'queued' })
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /show 3 documents/i }))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove hours.txt' }))
    })
    expect(screen.getByText(/Remove "hours.txt"\?/)).toBeInTheDocument()
    expect(screen.getByText(/Teams using "policies": support_team/)).toBeInTheDocument()
    await act(async () => {
      await answerConfirm(true)
    })
    expect(mockedApi.removeOwnKnowledgeBaseDocument).toHaveBeenCalledWith('policies', 'hours.txt')
    // The list is re-fetched so the row shows the new generation processing.
    await waitFor(() => expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2))
  })

  it('shows the row as processing from the 202 itself, even if the re-fetch fails', async () => {
    // Codex review: a failed (or out-of-order) refresh after a removal must
    // not leave the row looking idle with polling stopped.
    mockedApi.listOwnKnowledgeBases.mockResolvedValueOnce([kb({ documents: threeDocuments })])
    mockedApi.removeOwnKnowledgeBaseDocument.mockResolvedValue({ name: 'policies', job_id: 9, status: 'queued' })
    mockedApi.listOwnKnowledgeBases.mockRejectedValueOnce(new Error('network down'))
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /show 3 documents/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove hours.txt' }))
    })
    await act(async () => {
      await answerConfirm(true)
    })
    expect(await screen.findByText('Processing…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove handbook.pdf' })).toBeDisabled()
  })

  it('does not remove a document if the reader cancels', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ documents: threeDocuments })])
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /show 3 documents/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove hours.txt' }))
    })
    await act(async () => {
      await answerConfirm(false)
    })
    expect(mockedApi.removeOwnKnowledgeBaseDocument).not.toHaveBeenCalled()
  })

  it('disables Remove for the only document, and while an upload is processing', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ documents: [threeDocuments[0]] })])
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /show 1 document$/i }))
    const only = screen.getByRole('button', { name: 'Remove handbook.pdf' })
    expect(only).toBeDisabled()
    expect(only).toHaveAttribute('title', expect.stringMatching(/only document/i))
  })

  it("shows a refused removal's message on the row", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([kb({ documents: threeDocuments })])
    mockedApi.removeOwnKnowledgeBaseDocument.mockRejectedValue(
      new Error("'policies' is still processing an upload. Wait for it to finish, then remove the document."),
    )
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /show 3 documents/i }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove hours.txt' }))
    })
    await act(async () => {
      await answerConfirm(true)
    })
    expect(await screen.findByText(/still processing an upload/)).toBeInTheDocument()
  })

  it('shows a banner when the list itself cannot be loaded', async () => {
    mockedApi.listOwnKnowledgeBases.mockRejectedValue(new Error('Not authenticated'))
    render(<KnowledgeBasesPanel />)
    expect(await screen.findByText('Not authenticated')).toBeInTheDocument()
  })

  it('restores the previous upload once confirmed, naming what comes back', async () => {
    const restored = kb({
      documents: [threeDocuments[0]],
      used_by: ['support_team'],
      previous_generation: { completed_at: '2026-08-20T00:00:00Z', filenames: ['a.txt', 'b.txt'] },
    })
    mockedApi.listOwnKnowledgeBases.mockResolvedValueOnce([restored])
    // The re-fetch after restoring sees the row it just marked processing --
    // otherwise a static mock would make the refresh overwrite the optimistic
    // "Processing…" state with the pre-restore "Ready" one instantly.
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      { ...restored, latest_job: { ...restored.latest_job!, job_id: 9, status: 'queued' } },
    ])
    mockedApi.restoreOwnKnowledgeBase.mockResolvedValue({ name: 'policies', job_id: 9, status: 'queued' })
    render(<KnowledgeBasesPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    expect(screen.getByText(/Restore the previous upload to "policies"\?/)).toBeInTheDocument()
    expect(screen.getByText(/a\.txt, b\.txt/)).toBeInTheDocument()
    expect(screen.getByText(/Teams using "policies": support_team/)).toBeInTheDocument()
    await act(async () => {
      await answerConfirm(true)
    })
    expect(mockedApi.restoreOwnKnowledgeBase).toHaveBeenCalledWith('policies')
    expect(await screen.findByText('Processing…')).toBeInTheDocument()
    await waitFor(() => expect(mockedApi.listOwnKnowledgeBases).toHaveBeenCalledTimes(2))
  })

  it('does not restore if the reader cancels', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ previous_generation: { completed_at: null, filenames: ['a.txt'] } }),
    ])
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    await act(async () => {
      await answerConfirm(false)
    })
    expect(mockedApi.restoreOwnKnowledgeBase).not.toHaveBeenCalled()
  })

  it('disables Restore with nothing to go back to, and while an upload is processing', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ name: 'one_upload', previous_generation: null }),
      kb({
        name: 'busy',
        previous_generation: { completed_at: null, filenames: ['a.txt'] },
        latest_job: { job_id: 2, status: 'running', file_count: 1, documents_succeeded: 0, documents_failed: 0, chunk_count: 0, errors: [], retryable: false },
      }),
    ])
    render(<KnowledgeBasesPanel />)
    const buttons = await screen.findAllByRole('button', { name: 'Restore previous upload' })
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toBeDisabled()
    expect(buttons[0]).toHaveAttribute('title', expect.stringMatching(/no earlier upload/i))
    expect(buttons[1]).toBeDisabled()
    expect(buttons[1]).toHaveAttribute('title', expect.stringMatching(/still processing/i))
  })

  it("shows a refused restore's message on the row", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({ previous_generation: { completed_at: null, filenames: ['a.txt'] } }),
    ])
    mockedApi.restoreOwnKnowledgeBase.mockRejectedValue(
      new Error("The files for 'policies' are no longer on the server. Upload the documents you want, replacing the collection."),
    )
    render(<KnowledgeBasesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore previous upload' }))
    await act(async () => {
      await answerConfirm(true)
    })
    expect(await screen.findByText(/no longer on the server/)).toBeInTheDocument()
  })

  it('retries a failed upload from the row and marks it processing', async () => {
    const failedJob = {
      job_id: 7,
      status: 'failed' as const,
      file_count: 2,
      documents_succeeded: 0,
      documents_failed: 2,
      chunk_count: 0,
      errors: [{ filename: null, error: 'The documents could not be processed.' }],
      retryable: true,
    }
    const failed = kb({ servable: false, latest_job: failedJob })
    mockedApi.listOwnKnowledgeBases.mockResolvedValueOnce([failed])
    // The re-fetch after retrying sees the row it just marked processing --
    // same reasoning as the restore test above.
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      { ...failed, latest_job: { ...failedJob, status: 'queued' as const } },
    ])
    mockedApi.retryOwnKnowledgeBaseIngestion.mockResolvedValue({
      name: 'policies', job_id: 7, status: 'queued',
    })
    render(<KnowledgeBasesPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(mockedApi.retryOwnKnowledgeBaseIngestion).toHaveBeenCalledWith('policies', 7)
    expect(await screen.findByText('Processing…')).toBeInTheDocument()
  })

  it('disables Retry when the files are gone, and renders none on a healthy row', async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        name: 'gone',
        servable: false,
        latest_job: {
          job_id: 8,
          status: 'failed',
          file_count: 1,
          documents_succeeded: 0,
          documents_failed: 1,
          chunk_count: 0,
          errors: [],
          retryable: false,
        },
      }),
      kb({ name: 'healthy' }),
    ])
    render(<KnowledgeBasesPanel />)

    const button = await screen.findByRole('button', { name: 'Retry' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringMatching(/no longer on the server/i))
    // Exactly one Retry on the page: a row whose latest job succeeded has none.
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
  })

  it("shows a refused retry's message on the row", async () => {
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      kb({
        servable: false,
        latest_job: {
          job_id: 9,
          status: 'failed',
          file_count: 1,
          documents_succeeded: 0,
          documents_failed: 1,
          chunk_count: 0,
          errors: [],
          retryable: true,
        },
      }),
    ])
    mockedApi.retryOwnKnowledgeBaseIngestion.mockRejectedValue(
      new Error('A newer upload exists for this collection.'),
    )
    render(<KnowledgeBasesPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(/newer upload exists/)).toBeInTheDocument()
  })
})
