import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DocumentsPage from './DocumentsPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    orgKnowledgeBaseCapabilities: vi.fn(),
    uploadOwnKnowledgeBaseFiles: vi.fn(),
    orgKnowledgeBaseUploadJob: vi.fn(),
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

describe('DocumentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: freshSession(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.modelCatalog.mockResolvedValue([{ spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini' }])
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: false })
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
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('product_policies', [file], false, false),
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

    expect(screen.queryByText('Search quality')).not.toBeInTheDocument()
  })

  it('defaults to Enhanced and uploads with smart search on when available', async () => {
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Search quality')

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('policies', [file], false, true),
    )
  })

  it('uploads with smart search off when the customer switches to Standard', async () => {
    mockedApi.orgKnowledgeBaseCapabilities.mockResolvedValue({ smart_search_available: true })
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Search quality')
    fireEvent.click(screen.getByText('Standard'))

    fireEvent.change(screen.getByLabelText(/what should we call these documents/i), { target: { value: 'Policies' } })
    const file = new File(['x'], 'doc.txt', { type: 'text/plain' })
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() =>
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('policies', [file], false, false),
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
})
