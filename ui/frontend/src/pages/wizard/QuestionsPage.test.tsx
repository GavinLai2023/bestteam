import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import QuestionsPage from './QuestionsPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
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
      <QuestionsPage />
    </MemoryRouter>,
  )

const QUESTIONS = ['How many emails do you receive per day?', 'Which mailbox provider do you use?']

const sessionWithQuestions = (questions: string[] = QUESTIONS): BuilderSession => ({
  id: 's1',
  status: 'requirements',
  intent_text: 'reply to customer emails',
  requirements_json: {
    summary: 'Faster support',
    pain_points: [],
    goals: [],
    success_criteria: [],
    constraints: [],
    clarifying_questions: questions,
  },
  updated_at: '2026-08-24T00:00:00Z',
})

describe('QuestionsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = {
      session: sessionWithQuestions(),
      setSession: vi.fn(),
      loading: false,
      sessionId: 's1',
      setNavBusy: vi.fn(),
    }
    mockedApi.modelCatalog.mockResolvedValue([
      { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
    ])
  })

  it('renders one input per question, labelled by the question', () => {
    renderPage()

    expect(screen.getByLabelText(QUESTIONS[0])).toBeInTheDocument()
    expect(screen.getByLabelText(QUESTIONS[1])).toBeInTheDocument()
  })

  it('disables Continue until at least one answer is non-blank', async () => {
    renderPage()

    const button = screen.getByText('Continue').closest('button')!
    await waitFor(() => expect(screen.getByText('Skip these questions').closest('button')).toBeEnabled())
    expect(button).toBeDisabled()

    fireEvent.change(screen.getByLabelText(QUESTIONS[0]), { target: { value: 'About 40' } })
    expect(button).toBeEnabled()
  })

  it('sends the full paired batch and navigates to documents', async () => {
    mockedApi.submitRequirements.mockResolvedValue(sessionWithQuestions([]))
    renderPage()

    fireEvent.change(screen.getByLabelText(QUESTIONS[0]), { target: { value: '  About 40  ' } })
    const button = screen.getByText('Continue').closest('button')!
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)

    await waitFor(() =>
      expect(mockedApi.submitRequirements).toHaveBeenCalledWith('s1', {
        model: 'deepseek:friendly-assistant',
        answers: [
          { question: QUESTIONS[0], answer: 'About 40' },
          { question: QUESTIONS[1], answer: '' },
        ],
      }),
    )
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents')
  })

  it('skip sends every answer blank, even ones the customer typed', async () => {
    mockedApi.submitRequirements.mockResolvedValue(sessionWithQuestions([]))
    renderPage()

    fireEvent.change(screen.getByLabelText(QUESTIONS[0]), { target: { value: 'About 40' } })
    const skip = screen.getByText('Skip these questions').closest('button')!
    await waitFor(() => expect(skip).toBeEnabled())
    fireEvent.click(skip)

    await waitFor(() =>
      expect(mockedApi.submitRequirements).toHaveBeenCalledWith('s1', {
        model: 'deepseek:friendly-assistant',
        answers: [
          { question: QUESTIONS[0], answer: '' },
          { question: QUESTIONS[1], answer: '' },
        ],
      }),
    )
    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents')
  })

  it('shows the error and re-enables when the analyst call fails', async () => {
    mockedApi.submitRequirements.mockRejectedValue(new Error('analyst down'))
    renderPage()

    fireEvent.change(screen.getByLabelText(QUESTIONS[0]), { target: { value: 'About 40' } })
    const button = screen.getByText('Continue').closest('button')!
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)

    expect(await screen.findByText('analyst down')).toBeInTheDocument()
    expect(button).toBeEnabled()
    expect(navigateMock).not.toHaveBeenCalled()
    // The step bar was suspended for the call and released after the failure.
    expect(mockContext.setNavBusy).toHaveBeenCalledWith(true)
    expect(mockContext.setNavBusy).toHaveBeenLastCalledWith(false)
  })

  it('retrying a failed Skip stays a Skip, even with a typed answer on screen', async () => {
    mockedApi.submitRequirements
      .mockRejectedValueOnce(new Error('analyst down'))
      .mockResolvedValueOnce(sessionWithQuestions([]))
    renderPage()

    fireEvent.change(screen.getByLabelText(QUESTIONS[0]), { target: { value: 'About 40' } })
    const skip = screen.getByText('Skip these questions').closest('button')!
    await waitFor(() => expect(skip).toBeEnabled())
    fireEvent.click(skip)

    fireEvent.click(await screen.findByText('Try again'))

    await waitFor(() => expect(mockedApi.submitRequirements).toHaveBeenCalledTimes(2))
    const blankBatch = {
      model: 'deepseek:friendly-assistant',
      answers: [
        { question: QUESTIONS[0], answer: '' },
        { question: QUESTIONS[1], answer: '' },
      ],
    }
    expect(mockedApi.submitRequirements).toHaveBeenNthCalledWith(2, 's1', blankBatch)
  })

  it('with no open questions, offers Continue without calling the API', async () => {
    mockContext.session = sessionWithQuestions([])
    renderPage()

    expect(screen.getByText('No open questions — your description gave us what we need.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Continue'))

    expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents')
    expect(mockedApi.submitRequirements).not.toHaveBeenCalled()
  })
})
