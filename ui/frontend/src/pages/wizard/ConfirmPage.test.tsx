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

  it('enables Apply this change once the catalog loads, even with no described change', async () => {
    mockedApi.submitSolution.mockResolvedValue(sessionWithSpec())

    renderPage()

    const button = await screen.findByText('Apply this change')
    // The model catalog resolves asynchronously; the page picks the
    // Architect's model itself -- there's nothing for the customer to choose.
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

  // A customer doesn't choose which model parses their intent -- that's an
  // internal platform choice (the admin's is_default catalog entry). There is
  // no picker and no "Advanced settings" toggle to reveal one.
  it('never shows a model picker or an advanced-settings toggle', async () => {
    renderPage()

    await screen.findByText('Apply this change')
    expect(screen.queryByText('Advanced settings')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/assistant/i)).not.toBeInTheDocument()
  })

  // Every action on this page is gated on a chosen model, and ModelPicker
  // renders nothing when there is no catalog -- so without an explicit banner
  // the page is a dead end: a permanently disabled button and no stated
  // reason. IntentPage/DocumentsPage already guarded this; ConfirmPage didn't.
  describe('when no model is available', () => {
    it('explains an empty catalog instead of silently disabling everything', async () => {
      mockedApi.modelCatalog.mockResolvedValue([])

      renderPage()

      expect(await screen.findByText(/No AI models are available yet/i)).toBeInTheDocument()
      expect(screen.getByText('Apply this change').closest('button')).toBeDisabled()
    })

    it('explains a failed catalog fetch and offers a retry that recovers', async () => {
      mockedApi.modelCatalog.mockRejectedValueOnce(new Error('network down'))

      renderPage()

      expect(await screen.findByText(/Couldn't load the available AI models/i)).toBeInTheDocument()

      mockedApi.modelCatalog.mockResolvedValue([
        { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
      ])
      fireEvent.click(screen.getByText('Try again'))

      await waitFor(() =>
        expect(screen.queryByText(/Couldn't load the available AI models/i)).not.toBeInTheDocument(),
      )
      await waitFor(() =>
        expect(screen.getByText('Apply this change').closest('button')).toBeEnabled(),
      )
    })
  })

  // The wizard's best-effort Requirements call at intent-creation time can
  // fail silently (see core/requirements.py's generate_requirements) and
  // leave a session permanently stuck with requirements_json: null -- before
  // this, "No summary was generated for this session" was a dead end with no
  // way to generate one from here.
  describe('when no summary was generated for this session', () => {
    const sessionWithoutRequirements = (): BuilderSession => ({ ...sessionWithSpec(), requirements_json: undefined })

    it('offers a way to generate one instead of a dead end', async () => {
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1' }

      renderPage()
      fireEvent.click(await screen.findByText('Show what we understood about your business'))

      expect(screen.getByText('No summary was generated for this session.')).toBeInTheDocument()
      expect(await screen.findByText('Generate summary')).toBeInTheDocument()
    })

    it('generates and shows the summary once clicked', async () => {
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1' }
      const withRequirements = {
        ...sessionWithoutRequirements(),
        requirements_json: { summary: 'They want faster replies.', pain_points: [], goals: [], success_criteria: [], constraints: [], clarifying_questions: [] },
      }
      mockedApi.submitRequirements.mockResolvedValue(withRequirements)

      renderPage()
      fireEvent.click(await screen.findByText('Show what we understood about your business'))
      const button = await screen.findByText('Generate summary')
      await waitFor(() => expect(button.closest('button')).toBeEnabled())
      fireEvent.click(button)

      await waitFor(() =>
        expect(mockedApi.submitRequirements).toHaveBeenCalledWith('s1', { model: 'deepseek:friendly-assistant' }),
      )
      expect(mockContext.setSession).toHaveBeenCalledWith(withRequirements)
    })

    it('shows an error if generating the summary fails', async () => {
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1' }
      mockedApi.submitRequirements.mockRejectedValue(new Error('Model call failed'))

      renderPage()
      fireEvent.click(await screen.findByText('Show what we understood about your business'))
      const button = await screen.findByText('Generate summary')
      await waitFor(() => expect(button.closest('button')).toBeEnabled())
      fireEvent.click(button)

      expect(await screen.findByText('Model call failed')).toBeInTheDocument()
    })
  })
})
