import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DocumentsPage from './DocumentsPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'
import { answerAlternate, answerConfirm, confirmDialogBody } from '../../test/confirmDialog'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    orgKnowledgeBaseCapabilities: vi.fn(),
    uploadOwnKnowledgeBaseFiles: vi.fn(),
    orgKnowledgeBaseUploadJob: vi.fn(),
    listOwnKnowledgeBases: vi.fn(),
    removeOwnKnowledgeBaseDocument: vi.fn(),
    submitSpecification: vi.fn(),
    submitSolution: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const navigateMock = vi.fn()

let mockContext: {
  session: BuilderSession | null
  setSession: (session: BuilderSession) => void
  loading: boolean
  sessionId: string
}

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useOutletContext: () => mockContext, useNavigate: () => navigateMock }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <DocumentsPage />
    </MemoryRouter>,
  )

const freshSession = (): BuilderSession => ({
  id: 's1',
  status: 'requirements',
  intent_text: 'reply to customer emails',
  updated_at: '2026-08-09T00:00:00Z',
})

const sessionWithSpec = (): BuilderSession => ({
  id: 's1',
  status: 'spec',
  intent_text: 'reply to customer emails',
  specification_json: { name: 'support_workflow', agents: [], teams: [] },
  updated_at: '2026-08-09T00:00:00Z',
})

// A session whose agents already reference one or more knowledge-base tool
// names -- ground truth for which existing collection(s) this team searches.
const sessionUsingKbs = (...kbNames: string[]): BuilderSession => ({
  id: 's1',
  status: 'spec',
  intent_text: 'reply to customer emails',
  specification_json: {
    name: 'support_workflow',
    agents: [{ name: 'analyst', tools: kbNames }],
    teams: [],
  },
  updated_at: '2026-08-09T00:00:00Z',
})

const orgKb = (
  name: string,
  filenames: string[],
  extra: Partial<import('../../lib/types').OrgKnowledgeBase> = {},
): import('../../lib/types').OrgKnowledgeBase => ({
  name,
  description: null,
  type: 'local_folder',
  updated_at: '2026-08-09T00:00:00Z',
  used_by: [],
  servable: true,
  latest_job: null,
  documents: filenames.map((filename) => ({ filename, status: 'chunked', size_bytes: 42 })),
  previous_generation: null,
  ...extra,
})

const processingJob = {
  job_id: 7,
  status: 'running' as const,
  file_count: 1,
  documents_succeeded: 0,
  documents_failed: 0,
  chunk_count: 0,
  errors: [],
  retryable: false,
}

describe('DocumentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: freshSession(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.modelCatalog.mockResolvedValue([{ spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini' }])
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: false })
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([])
    mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1,
      status: 'completed',
      file_count: 1,
      documents_succeeded: 1,
      documents_failed: 0,
      chunk_count: 1,
      errors: [],
      config: {},
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('proceeds straight to spec generation when the user skips upload', async () => {
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.click(screen.getByText('Skip for now'))

    await waitFor(() => expect(mockedApi.submitSpecification).toHaveBeenCalledWith('s1', { model: 'openai:gpt-4o-mini' }))
    expect(mockedApi.uploadOwnKnowledgeBaseFiles).not.toHaveBeenCalled()
    expect(mockedApi.submitSolution).not.toHaveBeenCalled()
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('uploads the chosen files under the slugified label, then generates the spec with a KB hint', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'product_policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), {
      target: { value: 'Product Policies!' },
    })
    const file = new File(['refunds within 30 days'], 'policy.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })
    expect(screen.getByText('policy.txt')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('product_policies', [file], '', false, undefined),
    )
    // The architect only sees the org's whole KB catalog otherwise, which can
    // leave a fresh upload unattached if the org already has other
    // collections -- tell it explicitly which one the customer just added
    // (Codex review finding).
    expect(mockedApi.submitSpecification).toHaveBeenCalledWith('s1', {
      model: 'openai:gpt-4o-mini',
      feedback: expect.stringContaining('product_policies'),
    })
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('shows an error with a retry option when the upload fails, and does not generate a spec', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockRejectedValueOnce(new Error('Total upload size exceeds the limit'))
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValueOnce({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))
    await screen.findByText('Total upload size exceeds the limit')
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('Try again'))
    await waitFor(() => expect(mockedApi.submitSpecification).toHaveBeenCalled())
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('refines the existing design instead of regenerating it when documents are added after a specification already exists', async () => {
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))

    // Revisiting Documents from the Confirm page's "add or update documents"
    // link must refine the confirmed design, not regenerate one from scratch
    // -- regenerating silently discards any solution feedback already
    // applied (Codex review finding).
    await waitFor(() =>
      expect(mockedApi.submitSolution).toHaveBeenCalledWith('s1', {
        model: 'openai:gpt-4o-mini',
        feedback: expect.stringContaining('policies'),
      }),
    )
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
  })

  it('leaves an existing design untouched (empty feedback) when Skip is pressed with no files', async () => {
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.click(screen.getByText('Skip for now'))

    await waitFor(() =>
      expect(mockedApi.submitSolution).toHaveBeenCalledWith('s1', { model: 'openai:gpt-4o-mini', feedback: '' }),
    )
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
  })

  it('hides the search-quality toggle when smart search is not available', async () => {
    renderPage()
    await screen.findByText('Add your documents')
    await waitFor(() => expect(mockedApi.orgKnowledgeBaseCapabilities).toHaveBeenCalled())

    // With a file chosen, so the capability flag is the only thing that can be
    // hiding it -- the toggle is now gated on both.
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [new File(['x'], 'doc.txt', { type: 'text/plain' })] } })

    expect(screen.queryByText('Search quality')).not.toBeInTheDocument()
  })

  it('holds the search-quality toggle back until a file is chosen', async () => {
    // With nothing to upload the choice changes nothing: it is only ever read
    // by the upload call.
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })

    renderPage()
    await screen.findByText('Add your documents')
    await waitFor(() => expect(mockedApi.orgKnowledgeBaseCapabilities).toHaveBeenCalled())
    expect(screen.queryByText('Search quality')).not.toBeInTheDocument()

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [new File(['x'], 'doc.txt', { type: 'text/plain' })] } })

    expect(screen.getByText('Search quality')).toBeInTheDocument()
  })

  it('defaults to Enhanced and uploads with smart search on when available', async () => {
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })
    // Files first: the toggle renders only once there are some, and waiting for
    // it here is also what settles the capabilities request.
    await screen.findByText('Search quality')

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('policies', [file], '', true, undefined),
    )
  })

  it('uploads with smart search off when the customer switches to Standard', async () => {
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    await screen.findByText('Search quality')
    fireEvent.click(screen.getByText('Standard'))

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('policies', [file], '', false, undefined),
    )
  })

  it('polls the ingestion job to completion before generating the spec', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.orgKnowledgeBaseUploadJob
      .mockResolvedValueOnce({
        job_id: 1, status: 'running', file_count: 1, documents_succeeded: 0, documents_failed: 0,
        chunk_count: 0, errors: [], config: null,
      })
      .mockResolvedValueOnce({
        job_id: 1, status: 'completed', file_count: 1, documents_succeeded: 1, documents_failed: 0,
        chunk_count: 2, errors: [], config: {},
      })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    // Fake timers only from here on -- the poll loop's 500ms delay between
    // ingestion-job checks would otherwise hang this test on a real timer.
    vi.useFakeTimers()

    await act(async () => {
      fireEvent.click(screen.getByText('Continue'))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(mockedApi.orgKnowledgeBaseUploadJob).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    expect(mockedApi.orgKnowledgeBaseUploadJob).toHaveBeenCalledTimes(2)

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(mockedApi.submitSpecification).toHaveBeenCalled()
  })

  it('stops polling after the cap and shows a distinct still-processing notice', async () => {
    // Nothing reconciles a queued/running IngestionJob left behind by a
    // backend restart, so an uncapped poll left the wizard stuck on
    // "Processing your documents…" with no escape but a page reload.
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1, status: 'running', file_count: 1, documents_succeeded: 0, documents_failed: 0,
      chunk_count: 0, errors: [], config: null,
    })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(screen.getByText('Continue'))
      // Well past the cap (120 attempts x 500ms) -- the loop must have given
      // up rather than kept issuing requests.
      await vi.advanceTimersByTimeAsync(120 * 500 + 5000)
    })
    vi.useRealTimers()

    expect(mockedApi.orgKnowledgeBaseUploadJob).toHaveBeenCalledTimes(120)
    // Neither "succeeded" (no spec generated) nor "failed" (no error banner).
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
    expect(mockedApi.submitSolution).not.toHaveBeenCalled()
    expect(screen.getByText(/still being processed/i)).toBeInTheDocument()
    // Back to an interactive state rather than a permanent busy spinner.
    expect(screen.getByText('Continue')).toBeInTheDocument()
  })

  it('shows an error and does not generate a spec when the ingestion job fails', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1, status: 'failed', file_count: 1, documents_succeeded: 0, documents_failed: 1,
      chunk_count: 0, errors: [{ filename: 'doc.txt', error: 'could not parse' }], config: null,
    })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))

    await screen.findByText(/could not parse|processing failed/i)
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
  })

  it('sends the optional one-sentence description with the upload', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const descriptionInput = screen.getByLabelText(/what's in these documents/i)
    // The server caps the description at 500 characters; the input says so too.
    expect(descriptionInput).toHaveAttribute('maxlength', '500')
    fireEvent.change(descriptionInput, {
      target: { value: '  Our refund and shipping policies  ' },
    })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith(
        'policies',
        [file],
        '',
        false,
        'Our refund and shipping policies',
      ),
    )
  })

  async function raiseTheNameConflict() {
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    mockedApi.uploadOwnKnowledgeBaseFiles.mockRejectedValueOnce(
      Object.assign(new Error("'policies' already exists and may be used by another team. It currently uses Standard search. Choose a different name, add these documents to it, or replace what is in it."), { status: 409 }),
    )
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValueOnce({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })
    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })
    // The conflict dialog quotes the search quality, so wait for the toggle --
    // which needs the files above -- before Continue reads it.
    await screen.findByText('Search quality')

    fireEvent.click(screen.getByText('Continue'))
    return file
  }

  it('the name-conflict confirmation names the search quality that will be used', async () => {
    // The 409 detail says what the existing collection is like today; the
    // confirmation adds what it would become, so both halves of the change
    // are in the one dialog the customer has to answer.
    const file = await raiseTheNameConflict()

    const body = await confirmDialogBody()
    expect(body).toContain('It currently uses Standard search.')
    expect(body).toContain('re-indexed with Enhanced search.')

    await act(async () => {
      await answerConfirm(true)
    })

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenLastCalledWith('policies', [file], 'replace', true, undefined),
    )
  })

  it('offers adding to the existing collection, not only replacing it', async () => {
    // Adding is the common case: before it existed, keeping the documents
    // already in a collection meant finding and re-uploading all of them.
    const file = await raiseTheNameConflict()
    await screen.findByText('Add to it')

    await act(async () => {
      await answerAlternate()
    })

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenLastCalledWith('policies', [file], 'add', true, undefined),
    )
  })

  it('uploads nothing when the name conflict is cancelled', async () => {
    await raiseTheNameConflict()
    await screen.findByText('Add to it')

    await act(async () => {
      await answerConfirm(false)
    })

    expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledTimes(1)
  })

  it('prefills the name and lists the files already there when the team uses exactly one existing collection', async () => {
    mockContext = { session: sessionUsingKbs('product_policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([orgKb('product_policies', ['policy.txt'])])

    renderPage()
    await screen.findByText('Add your documents')

    await waitFor(() =>
      expect(screen.getByLabelText(/what should we call these documents/i)).toHaveValue('product_policies'),
    )
    expect(await screen.findByText('Files already in "product_policies"')).toBeInTheDocument()
    expect(screen.getByText('policy.txt')).toBeInTheDocument()
  })

  it('offers a picker instead of guessing when the team uses more than one existing collection', async () => {
    mockContext = {
      session: sessionUsingKbs('policies_a', 'policies_b'),
      setSession: vi.fn(),
      loading: false,
      sessionId: 's1',
    }
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      orgKb('policies_a', ['a.txt']),
      orgKb('policies_b', ['b.txt']),
    ])

    renderPage()
    await screen.findByText('Add your documents')

    await screen.findByText('Your team already searches more than one collection. Which one are you updating?')
    expect(screen.getByLabelText(/what should we call these documents/i)).toHaveValue('')
    expect(screen.queryByText('a.txt')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('policies_b'))

    await waitFor(() =>
      expect(screen.getByLabelText(/what should we call these documents/i)).toHaveValue('policies_b'),
    )
    expect(screen.getByText('b.txt')).toBeInTheDocument()
  })

  it('removes a file already in the collection and refreshes the list', async () => {
    mockContext = { session: sessionUsingKbs('product_policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    // Reset rather than chain onto whatever's left in the queue -- a prior
    // test's unconsumed `mockResolvedValueOnce` would otherwise leak in,
    // since `vi.clearAllMocks()` in beforeEach clears call history but not
    // queued once-implementations.
    mockedApi.listOwnKnowledgeBases.mockReset()
    // Two documents, because removing the last readable one is a 409 the
    // page now refuses up front.
    mockedApi.listOwnKnowledgeBases
      .mockResolvedValueOnce([orgKb('product_policies', ['policy.txt', 'keepme.txt'])])
      .mockResolvedValueOnce([orgKb('product_policies', ['keepme.txt'])])
    mockedApi.removeOwnKnowledgeBaseDocument.mockResolvedValue({ name: 'product_policies', job_id: 9, status: 'queued' })
    mockedApi.orgKnowledgeBaseUploadJob.mockResolvedValue({
      job_id: 9, status: 'completed', file_count: 0, documents_succeeded: 0, documents_failed: 0,
      chunk_count: 0, errors: [], config: {},
    })

    renderPage()
    await screen.findByText('Files already in "product_policies"')

    fireEvent.click(screen.getByRole('button', { name: /remove policy\.txt/i }))
    await screen.findByText('Cancel')
    await act(async () => {
      await answerConfirm(true)
    })

    await waitFor(() =>
      expect(mockedApi.removeOwnKnowledgeBaseDocument).toHaveBeenCalledWith('product_policies', 'policy.txt'),
    )
    await waitFor(() => expect(screen.queryByText('policy.txt')).not.toBeInTheDocument())
  })

  it('pauses after uploading to an existing collection to show the merged file list before continuing', async () => {
    mockContext = { session: sessionUsingKbs('policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    // Reset rather than chain onto whatever's left in the queue -- see the
    // comment on the same call in the previous test.
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases
      .mockResolvedValueOnce([orgKb('policies', ['oldfile.txt'])])
      .mockResolvedValueOnce([orgKb('policies', ['oldfile.txt', 'newfile.txt'])])
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    // Reset first -- a cancelled name-conflict dialog in an earlier test
    // deliberately leaves its retry value unconsumed in the queue (it never
    // gets that far), and `vi.clearAllMocks()` doesn't drain queued
    // once-implementations, only call history.
    mockedApi.uploadOwnKnowledgeBaseFiles.mockReset()
    mockedApi.uploadOwnKnowledgeBaseFiles.mockRejectedValueOnce(
      Object.assign(new Error("'policies' already exists."), { status: 409 }),
    )
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValueOnce({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSolution.mockResolvedValue(sessionUsingKbs('policies'))

    renderPage()
    await screen.findByText('Files already in "policies"')

    const file = new File(['x'], 'newfile.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))
    // Wait for the name-conflict dialog outside `act` (as the other tests
    // using this helper do) before answering it inside one.
    await screen.findByText('Cancel')
    await act(async () => {
      await answerConfirm(true) // "Replace everything" / "Add to it" dialog
    })

    await screen.findByText('Here’s what "policies" contains now')
    expect(screen.getByText('oldfile.txt')).toBeInTheDocument()
    expect(screen.getByText('newfile.txt')).toBeInTheDocument()
    expect(mockedApi.submitSolution).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('Continue'))
    await waitFor(() => expect(mockedApi.submitSolution).toHaveBeenCalled())
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('keeps an existing collection’s own name instead of slugifying it into a different one', async () => {
    // The server's charset allows hyphens and capitals, so a collection can
    // legally be named `support-docs`. Slugifying a name that already exists
    // points the page (and the upload) at `support_docs` -- a different,
    // non-existent collection (Codex review finding).
    mockContext = { session: sessionUsingKbs('support-docs'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([orgKb('support-docs', ['policy.txt', 'faq.txt'])])
    mockedApi.uploadOwnKnowledgeBaseFiles.mockReset()
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'support-docs', job_id: 1, status: 'queued' })

    renderPage()
    await screen.findByText('Files already in "support-docs"')
    expect(screen.getByLabelText(/what should we call these documents/i)).toHaveValue('support-docs')

    const file = new File(['x'], 'newfile.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })
    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('support-docs', [file], '', false, undefined),
    )
  })

  it('disables Remove for the only readable document instead of offering a call the backend refuses', async () => {
    mockContext = { session: sessionUsingKbs('policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([orgKb('policies', ['policy.txt'])])

    renderPage()
    await screen.findByText('Files already in "policies"')

    const button = screen.getByRole('button', { name: /remove policy\.txt/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringContaining('only document') as unknown as string)
  })

  it('disables Remove while the collection is still processing an upload', async () => {
    mockContext = { session: sessionUsingKbs('policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      orgKb('policies', ['a.txt', 'b.txt'], { latest_job: processingJob }),
    ])

    renderPage()
    await screen.findByText('Files already in "policies"')

    expect(screen.getByRole('button', { name: /remove a\.txt/i })).toBeDisabled()
  })

  it('names every team that loses the document, not just this one', async () => {
    // A collection is shared: removing a document changes the answers of
    // every team searching it, so the confirmation has to name them
    // (Codex review finding).
    mockContext = { session: sessionUsingKbs('policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([
      orgKb('policies', ['a.txt', 'b.txt'], { used_by: ['Support team', 'Sales team'] }),
    ])

    renderPage()
    await screen.findByText('Files already in "policies"')

    fireEvent.click(screen.getByRole('button', { name: /remove a\.txt/i }))
    const body = await confirmDialogBody()
    expect(body).toContain('Support team')
    expect(body).toContain('Sales team')
  })

  it('still pauses for the merged review when the collection is only discovered by the name conflict', async () => {
    // The list request can fail or still be in flight when the upload starts;
    // the 409 then proves the collection already exists, so the promised
    // review must not be skipped (Codex review finding).
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue([orgKb('policies', ['oldfile.txt', 'newfile.txt'])])
    mockedApi.uploadOwnKnowledgeBaseFiles.mockReset()
    mockedApi.uploadOwnKnowledgeBaseFiles.mockRejectedValueOnce(
      Object.assign(new Error("'policies' already exists."), { status: 409 }),
    )
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValueOnce({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'newfile.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))
    await screen.findByText('Cancel')
    await act(async () => {
      await answerConfirm(true)
    })

    await screen.findByText('Here’s what "policies" contains now')
    expect(mockedApi.submitSolution).not.toHaveBeenCalled()
  })

  it('does not offer a retry that generates a spec when removing a document fails', async () => {
    // The page-wide error banner's "Try again" calls proceed(), which makes a
    // billable model call -- the wrong action entirely for a failed removal
    // (Codex review finding).
    mockContext = { session: sessionUsingKbs('policies'), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.listOwnKnowledgeBases.mockReset()
    mockedApi.listOwnKnowledgeBases.mockResolvedValue([orgKb('policies', ['a.txt', 'b.txt'])])
    mockedApi.removeOwnKnowledgeBaseDocument.mockRejectedValue(new Error('could not remove it'))

    renderPage()
    await screen.findByText('Files already in "policies"')

    fireEvent.click(screen.getByRole('button', { name: /remove a\.txt/i }))
    await screen.findByText('Cancel')
    await act(async () => {
      await answerConfirm(true)
    })

    await screen.findByText('could not remove it')
    expect(screen.queryByText('Try again')).not.toBeInTheDocument()
    expect(mockedApi.submitSolution).not.toHaveBeenCalled()
    expect(mockedApi.submitSpecification).not.toHaveBeenCalled()
  })
})
