import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ConfirmPage from './ConfirmPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    submitSolution: vi.fn(),
    submitRequirements: vi.fn(),
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
      <ConfirmPage />
    </MemoryRouter>,
  )

const deployedSpec = { name: 'support_workflow', agents: [], teams: [] }

const sessionWithSpec = (): BuilderSession => ({
  id: 's1',
  status: 'spec',
  intent_text: 'reply to customer emails',
  specification_json: deployedSpec,
  updated_at: '2026-08-09T00:00:00Z',
})

describe('ConfirmPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.modelCatalog.mockResolvedValue([{ spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' }])
  })

  it('enables Apply this change once a model is picked, even with no described change', async () => {
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()

    const button = await screen.findByText('Apply this change')
    // The model catalog resolves asynchronously and auto-picks a default --
    // wait for that before asserting the button is enabled.
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.submitSolution).toHaveBeenCalledWith('s1', {
        feedback: '',
        model: 'deepseek:friendly-assistant',
      }),
    )
  })

  it('sends the typed feedback alongside the picked model', async () => {
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()
    const button = await screen.findByText('Apply this change')
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.change(screen.getByPlaceholderText(/Have the team check our FAQ/i), {
      target: { value: 'Make replies friendlier' },
    })
    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.submitSolution).toHaveBeenCalledWith('s1', {
        feedback: 'Make replies friendlier',
        model: 'deepseek:friendly-assistant',
      }),
    )
  })

  it('navigates to the documents step to upload or update a document', async () => {
    renderPage()

    fireEvent.click(await screen.findByText('Need to add or update a document? Upload it here'))

    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents')
  })
})
