import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DocumentsPage from './DocumentsPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    uploadOwnKnowledgeBaseFiles: vi.fn(),
    submitSpecification: vi.fn(),
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

describe('DocumentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: freshSession(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.modelCatalog.mockResolvedValue([{ spec: 'openai:gpt-4o-mini', display_name: 'GPT-4o mini' }])
  })

  it('proceeds straight to spec generation when the user skips upload', async () => {
    mockedApi.submitSpecification.mockResolvedValue({ ...freshSession(), specification_json: { name: 't', agents: [], teams: [] } })

    renderPage()
    await screen.findByText('Add your documents')

    fireEvent.click(screen.getByText('Skip for now'))

    await waitFor(() => expect(mockedApi.submitSpecification).toHaveBeenCalledWith('s1', { model: 'openai:gpt-4o-mini' }))
    expect(mockedApi.uploadOwnKnowledgeBaseFiles).not.toHaveBeenCalled()
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('uploads the chosen files under the slugified label, then generates the spec', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValue({ name: 'product_policies', file_count: 1, chunk_count: 2, config: {} })
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
      expect(mockedApi.uploadOwnKnowledgeBaseFiles).toHaveBeenCalledWith('product_policies', [file]),
    )
    expect(mockedApi.submitSpecification).toHaveBeenCalledWith('s1', { model: 'openai:gpt-4o-mini' })
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/preview')
  })

  it('shows an error with a retry option when the upload fails, and does not generate a spec', async () => {
    mockedApi.uploadOwnKnowledgeBaseFiles.mockRejectedValueOnce(new Error('Total upload size exceeds the limit'))
    mockedApi.uploadOwnKnowledgeBaseFiles.mockResolvedValueOnce({ name: 'policies', file_count: 1, chunk_count: 1, config: {} })
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
})
