import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ConfirmPage from './ConfirmPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    refineTeam: vi.fn(),
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
  setNavBusy: ReturnType<typeof vi.fn>
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
    mockContext = { session: sessionWithSpec(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
    mockedApi.modelCatalog.mockResolvedValue([{ spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' }])
  })

  it('enables Update the team once the catalog loads, even with no described change', async () => {
    mockedApi.refineTeam.mockResolvedValue(sessionWithSpec())

    renderPage()

    const button = await screen.findByText('Update the team')
    // The model catalog resolves asynchronously; the page picks the
    // Architect's model itself -- there's nothing for the customer to choose.
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.refineTeam).toHaveBeenCalledWith('s1', {
        feedback: '',
        model: 'deepseek:friendly-assistant',
      }),
    )
  })

  it('sends the typed feedback alongside the picked model', async () => {
    mockedApi.refineTeam.mockResolvedValue(sessionWithSpec())

    renderPage()
    const button = await screen.findByText('Update the team')
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.change(screen.getByPlaceholderText(/FAQ document/i), {
      target: { value: 'Make replies friendlier' },
    })
    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.refineTeam).toHaveBeenCalledWith('s1', {
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

    await screen.findByText('Update the team')
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
      expect(screen.getByText('Update the team').closest('button')).toBeDisabled()
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
        expect(screen.getByText('Update the team').closest('button')).toBeEnabled(),
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
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }

      renderPage()

      expect(screen.getByText('No summary was generated for this session.')).toBeInTheDocument()
      expect(await screen.findByText('Generate summary')).toBeInTheDocument()
    })

    it('generates and shows the summary once clicked', async () => {
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
      const withRequirements = {
        ...sessionWithoutRequirements(),
        requirements_json: { summary: 'They want faster replies.', pain_points: [], goals: [], success_criteria: [], constraints: [], clarifying_questions: [] },
      }
      mockedApi.submitRequirements.mockResolvedValue(withRequirements)

      renderPage()
      const button = await screen.findByText('Generate summary')
      await waitFor(() => expect(button.closest('button')).toBeEnabled())
      fireEvent.click(button)

      await waitFor(() =>
        expect(mockedApi.submitRequirements).toHaveBeenCalledWith('s1', { model: 'deepseek:friendly-assistant' }),
      )
      expect(mockContext.setSession).toHaveBeenCalledWith(withRequirements)
    })

    it('shows an error if generating the summary fails', async () => {
      mockContext = { session: sessionWithoutRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
      mockedApi.submitRequirements.mockRejectedValue(new Error('Model call failed'))

      renderPage()
      const button = await screen.findByText('Generate summary')
      await waitFor(() => expect(button.closest('button')).toBeEnabled())
      fireEvent.click(button)

      expect(await screen.findByText('Model call failed')).toBeInTheDocument()
    })
  })
})

describe('ConfirmPage layout', () => {
  const sessionWithRequirements = (): BuilderSession => ({
    ...sessionWithSpec(),
    requirements_json: {
      summary: 'They answer payroll questions by hand.',
      pain_points: [],
      goals: [],
      success_criteria: [],
      constraints: [],
      clarifying_questions: [],
    },
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: sessionWithRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
    mockedApi.modelCatalog.mockResolvedValue([
      { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
    ])
  })

  // The understanding is what the team design is derived from, so a customer
  // who wants to correct something should meet it before the thing it
  // produced. Collapsed by default, most never saw it at all.
  it('shows what we understood without needing a click', async () => {
    renderPage()

    expect(await screen.findByDisplayValue('They answer payroll questions by hand.')).toBeInTheDocument()
  })

  it('puts what we understood above the team it produced', async () => {
    renderPage()

    const understanding = await screen.findByText(/what we understood about your business/i)
    const team = await screen.findByText('Your team')

    expect(understanding.compareDocumentPosition(team) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

// The page used to carry three buttons -- Save this summary, Regenerate
// summary and Update the team -- whose effects a customer could not tell
// apart, and two of which could destroy the third's work. There is one
// action now, and it carries everything the customer touched.
describe('ConfirmPage single update action', () => {
  const sessionWithRequirements = (): BuilderSession => ({
    ...sessionWithSpec(),
    requirements_json: {
      summary: 'They answer payroll questions by hand.',
      pain_points: [],
      goals: [],
      success_criteria: [],
      constraints: [],
      clarifying_questions: [],
    },
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: sessionWithRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
    mockedApi.modelCatalog.mockResolvedValue([
      { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
    ])
    mockedApi.refineTeam.mockResolvedValue(sessionWithRequirements())
  })

  it('sends the hand-edited fields and the described change together', async () => {
    renderPage()
    const button = await screen.findByText('Update the team')
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.change(screen.getByDisplayValue('They answer payroll questions by hand.'), {
      target: { value: 'They answer payroll questions by hand, slowly.' },
    })
    fireEvent.change(screen.getByPlaceholderText(/FAQ document/i), {
      target: { value: 'Keep replies under 150 words.' },
    })
    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.refineTeam).toHaveBeenCalledWith('s1', {
        requirements: expect.objectContaining({ summary: 'They answer payroll questions by hand, slowly.' }),
        feedback: 'Keep replies under 150 words.',
        model: 'deepseek:friendly-assistant',
      }),
    )
  })

  it('carries a hand-edited field even when nothing is described in words', async () => {
    renderPage()
    const button = await screen.findByText('Update the team')
    await waitFor(() => expect(button.closest('button')).toBeEnabled())

    fireEvent.change(screen.getByDisplayValue('They answer payroll questions by hand.'), {
      target: { value: 'They answer payroll questions by hand, slowly.' },
    })
    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.refineTeam).toHaveBeenCalledWith('s1', {
        requirements: expect.objectContaining({ summary: 'They answer payroll questions by hand, slowly.' }),
        feedback: '',
        model: 'deepseek:friendly-assistant',
      }),
    )
  })

  it('offers no separate save or regenerate button', async () => {
    renderPage()

    await screen.findByText('Update the team')
    expect(screen.queryByText('Save this summary')).not.toBeInTheDocument()
    expect(screen.queryByText('Regenerate summary')).not.toBeInTheDocument()
  })

  it('offers one place to describe a change, not two', async () => {
    renderPage()

    await screen.findByText('Update the team')
    expect(screen.queryByLabelText(/Not quite right/i)).not.toBeInTheDocument()
  })

  it('puts the change box below the team, since it changes both', async () => {
    renderPage()

    const team = await screen.findByText('Your team')
    const box = screen.getByPlaceholderText(/FAQ document/i)

    expect(team.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

// Two model calls run behind the one button, so the wait is long enough that
// a customer will look for something else to do. Nothing on the page should
// take a click while it is going on -- least of all "Continue to deploy",
// which would publish the team the Architect is in the middle of replacing.
describe('ConfirmPage while the update is in flight', () => {
  const sessionWithRequirements = (): BuilderSession => ({
    ...sessionWithSpec(),
    requirements_json: {
      summary: 'They answer payroll questions by hand.',
      pain_points: ['replies take days'],
      goals: [],
      success_criteria: [],
      constraints: [],
      clarifying_questions: [],
    },
  })

  let resolveRefine: (session: BuilderSession) => void

  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: sessionWithRequirements(), setSession: vi.fn(), loading: false, sessionId: 's1', setNavBusy: vi.fn() }
    mockedApi.modelCatalog.mockResolvedValue([
      { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
    ])
    mockedApi.refineTeam.mockReturnValue(
      new Promise<BuilderSession>((resolve) => {
        resolveRefine = resolve
      }),
    )
  })

  const startUpdate = async () => {
    renderPage()
    const button = await screen.findByText('Update the team')
    await waitFor(() => expect(button.closest('button')).toBeEnabled())
    fireEvent.click(button)
    return button
  }

  it('asks the customer to wait instead of leaving them guessing', async () => {
    await startUpdate()

    expect(await screen.findByText(/stay on this page/i)).toBeInTheDocument()
  })

  it('takes no further input from anything on the page', async () => {
    await startUpdate()

    await waitFor(() => expect(screen.getByPlaceholderText(/FAQ document/i)).toBeDisabled())
    expect(screen.getByDisplayValue('They answer payroll questions by hand.')).toBeDisabled()
    expect(screen.getByDisplayValue('replies take days')).toBeDisabled()
    expect(screen.getAllByText('+ add')[0].closest('button')).toBeDisabled()
    expect(screen.getByText('Need to add or update a document? Upload it here').closest('button')).toBeDisabled()
    expect(screen.getByText('Back to preview').closest('button')).toBeDisabled()
    expect(screen.getByText('Continue to deploy').closest('button')).toBeDisabled()
  })

  it('tells the wizard chrome to stop offering step links, and to start again after', async () => {
    await startUpdate()

    await waitFor(() => expect(mockContext.setNavBusy).toHaveBeenCalledWith(true))

    resolveRefine(sessionWithRequirements())

    await waitFor(() => expect(mockContext.setNavBusy).toHaveBeenLastCalledWith(false))
  })

  it('gives the page back once the team has been updated', async () => {
    await startUpdate()
    await screen.findByText(/stay on this page/i)

    resolveRefine(sessionWithRequirements())

    await waitFor(() => expect(screen.queryByText(/stay on this page/i)).not.toBeInTheDocument())
    expect(screen.getByText('Continue to deploy').closest('button')).toBeEnabled()
    expect(screen.getByPlaceholderText(/FAQ document/i)).toBeEnabled()
  })
})
