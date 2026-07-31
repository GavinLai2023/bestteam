import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionsPage from './SessionsPage'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({
  api: {
    listSessions: vi.fn(),
    getEmailTrigger: vi.fn(),
    deleteSession: vi.fn(),
  },
}))

const renderPage = () =>
  render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  )

const session = (overrides = {}) => ({
  id: 's1',
  status: 'deployed',
  intent_text: 'do stuff',
  specification_json: { name: 'my-team' },
  updated_at: '2026-07-31T00:00:00Z',
  ...overrides,
})

describe('SessionsPage automation tag', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a one-line automation tag on the matching team card when the trigger is enabled', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'my-team',
      status: 'active',
      last_checked_at: '2026-07-31T11:02:00Z',
    })

    renderPage()

    expect(await screen.findByText(/Automation on/)).toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger targets a different team', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'some-other-team',
      status: 'active',
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation on/)).not.toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger is disabled', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })

  it('does not render the old full automatic-runs history block', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockResolvedValue({
      enabled: true,
      workflow_name: 'my-team',
      status: 'active',
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automatic runs —/)).not.toBeInTheDocument()
  })

  it('degrades gracefully (no tag, no crash) if fetching the trigger fails', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] })
    api.getEmailTrigger.mockRejectedValue(new Error('boom'))

    renderPage()

    expect(await screen.findByText('my-team')).toBeInTheDocument()
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })
})

describe('SessionsPage date format', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it('shows the updated time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ updated_at: '2026-07-31T14:05:00' })] })

    renderPage()

    expect(await screen.findByText(/31 JUL 2026, 2:05 PM/)).toBeInTheDocument()
  })
})

describe('SessionsPage card description', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it("shows the team's friendly description instead of the raw intent text", async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session({
          specification_json: {
            name: 'my-team',
            teams: [{ name: 't1', friendly_description: 'Reads and replies to customer emails automatically.' }],
          },
        }),
      ],
    })

    renderPage()

    expect(await screen.findByText('Reads and replies to customer emails automatically.')).toBeInTheDocument()
    expect(screen.queryByText('do stuff')).not.toBeInTheDocument()
  })

  it('falls back to the intent text when no team description is available yet', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session()] }) // specification_json has no teams

    renderPage()

    expect(await screen.findByText('do stuff')).toBeInTheDocument()
  })
})

describe('SessionsPage status grouping', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it('groups sessions into headed sections ordered Deployed, Testing, Solution, Spec', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session({ id: 's1', status: 'spec', specification_json: { name: 'spec-team' } }),
        session({ id: 's2', status: 'deployed', specification_json: { name: 'deployed-team' } }),
        session({ id: 's3', status: 'testing', specification_json: { name: 'testing-team' } }),
        session({ id: 's4', status: 'solution', specification_json: { name: 'solution-team' } }),
      ],
    })

    renderPage()
    await screen.findByText('deployed-team')

    const sectionHeadings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(sectionHeadings).toEqual(['Deployed (1)', 'Testing (1)', 'Solution (1)', 'Spec (1)'])
  })

  it('omits section headers for statuses with no sessions', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ status: 'deployed' })] })

    renderPage()
    await screen.findByText('my-team')

    const sectionHeadings = screen.getAllByRole('heading', { level: 2 })
    expect(sectionHeadings).toHaveLength(1)
    expect(sectionHeadings[0]).toHaveTextContent('Deployed (1)')
  })
})

describe('SessionsPage status explanations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it('clicking the status help button toggles its explanation open and closed', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ status: 'deployed' })] })

    renderPage()
    await screen.findByText('my-team')

    const helpButton = screen.getByRole('button', { name: /what does deployed mean/i })
    expect(screen.queryByText(/deployed and ready/i)).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(helpButton)
    })
    expect(screen.getByText(/deployed and ready/i)).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(helpButton)
    })
    expect(screen.queryByText(/deployed and ready/i)).not.toBeInTheDocument()
  })
})

describe('SessionsPage draft deletion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEmailTrigger.mockResolvedValue({ enabled: false, workflow_name: null, status: 'disabled' })
  })

  it('shows a Delete button for a session that was never deployed', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })

    renderPage()

    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('does not show a Delete button for a session linked to a live team', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: 7 })] })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('does nothing if the user cancels the confirmation', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(api.deleteSession).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('deletes the session and removes its card when confirmed', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ id: 's1', workflow_id: null })] })
    api.deleteSession.mockResolvedValue(null)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(api.deleteSession).toHaveBeenCalledWith('s1')
    await waitFor(() => expect(screen.queryByText('my-team')).not.toBeInTheDocument())
  })

  it('shows an error banner and keeps the card if deletion fails', async () => {
    api.listSessions.mockResolvedValue({ sessions: [session({ workflow_id: null })] })
    api.deleteSession.mockRejectedValue(new Error("Can't delete right now"))
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(await screen.findByText("Can't delete right now")).toBeInTheDocument()
    expect(screen.getByText('my-team')).toBeInTheDocument()
  })
})
