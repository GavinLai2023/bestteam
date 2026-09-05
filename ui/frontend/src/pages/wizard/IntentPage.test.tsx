import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IntentPage from './IntentPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    modelCatalog: vi.fn(),
    createSession: vi.fn(),
    submitRequirements: vi.fn(),
    transcribeInterview: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const navigateMock = vi.fn()

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => navigateMock }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <IntentPage />
    </MemoryRouter>,
  )

const sessionWith = (questions: string[]): BuilderSession => ({
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

const start = async () => {
  fireEvent.change(screen.getByLabelText('What do you want help with?'), {
    target: { value: 'We handle customer support emails.' },
  })
  const button = await screen.findByRole('button', { name: 'Start building my team' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
}

describe('IntentPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.modelCatalog.mockResolvedValue([
      { spec: 'deepseek:friendly-assistant', display_name: 'Friendly Assistant' },
    ])
    mockedApi.createSession.mockResolvedValue(sessionWith([]))
  })

  it('hands over to the interview when the analyst has questions', async () => {
    mockedApi.submitRequirements.mockResolvedValue(sessionWith(['How many emails per day?']))
    renderPage()

    await start()

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/questions'))
  })

  it('goes straight to documents when there is nothing to ask', async () => {
    mockedApi.submitRequirements.mockResolvedValue(sessionWith([]))
    renderPage()

    await start()

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents'))
  })

  it('still reaches documents when the requirements call fails (best-effort)', async () => {
    mockedApi.submitRequirements.mockRejectedValue(new Error('analyst down'))
    renderPage()

    await start()

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents'))
  })

  it('keeps a busy notice on screen while the analyst is working', async () => {
    let finish: (session: BuilderSession) => void = () => {}
    mockedApi.submitRequirements.mockReturnValue(
      new Promise<BuilderSession>((resolve) => {
        finish = resolve
      }),
    )
    renderPage()

    expect(screen.queryByRole('status')).toBeNull()

    await start()

    expect(await screen.findByRole('status')).toBeInTheDocument()

    finish(sessionWith([]))

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents'))
  })
})
