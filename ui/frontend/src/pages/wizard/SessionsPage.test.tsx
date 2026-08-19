import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionsPage from './SessionsPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    listSessions: vi.fn(),
    getEmailTrigger: vi.fn(),
    deleteSession: vi.fn(),
    listOwnKnowledgeBases: vi.fn().mockResolvedValue([]),
  },
}))

const mockedApi = vi.mocked(api)

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  )

const session = (overrides: Partial<BuilderSession> = {}): BuilderSession => ({
  id: 's1',
  status: 'deployed',
  intent_text: 'do stuff',
  specification_json: { name: 'my-team', agents: [], teams: [] },
  updated_at: '2026-07-31T00:00:00Z',
  ...overrides,
})

describe('SessionsPage automation tag', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a one-line automation tag on the matching team card when the trigger is enabled', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] })
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'my-team',
      status: 'active',
      daily_cap: 0,
      last_checked_at: '2026-07-31T11:02:00Z',
    })

    renderPage()

    expect(await screen.findByText(/Automation on/)).toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger targets a different team', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] })
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'some-other-team',
      status: 'active',
      daily_cap: 0,
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation on/)).not.toBeInTheDocument()
  })

  it('does not show an automation tag when the trigger is disabled', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] })
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: false,
      pipeline_name: null,
      status: 'disabled',
      daily_cap: 0,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })

  it('does not render the old full automatic-runs history block', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] })
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: true,
      pipeline_name: 'my-team',
      status: 'active',
      daily_cap: 0,
      last_checked_at: null,
    })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByText(/Automatic runs —/)).not.toBeInTheDocument()
  })

  it('degrades gracefully (no tag, no crash) if fetching the trigger fails', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] })
    mockedApi.getEmailTrigger.mockRejectedValue(new Error('boom'))

    renderPage()

    expect(await screen.findByText('my-team')).toBeInTheDocument()
    expect(screen.queryByText(/Automation/)).not.toBeInTheDocument()
  })
})

describe('SessionsPage date format', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it('shows the updated time as "DD MMM YYYY, h:mm AM/PM"', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ updated_at: '2026-07-31T14:05:00' })] })

    renderPage()

    expect(await screen.findByText(/31 JUL 2026, 2:05 PM/)).toBeInTheDocument()
  })
})

describe('SessionsPage card description', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it("shows the team's friendly description instead of the raw intent text", async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [
        session({
          specification_json: {
            name: 'my-team',
            agents: [],
            teams: [{ name: 't1', mode: 'sequential', agents: [], friendly_description: 'Reads and replies to customer emails automatically.' }],
          },
        }),
      ],
    })

    renderPage()

    expect(await screen.findByText('Reads and replies to customer emails automatically.')).toBeInTheDocument()
    expect(screen.queryByText('do stuff')).not.toBeInTheDocument()
  })

  it('falls back to the intent text when no team description is available yet', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session()] }) // specification_json has no teams

    renderPage()

    expect(await screen.findByText('do stuff')).toBeInTheDocument()
  })
})

describe('SessionsPage card title', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it("shows the team's customer-facing display_name instead of the internal technical name", async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [
        session({
          specification_json: {
            name: 'customer_support_team',
            agents: [],
            teams: [{ name: 't1', mode: 'sequential', agents: [], display_name: 'Customer Support Team' }],
          },
        }),
      ],
    })

    renderPage()

    expect(await screen.findByText('Customer Support Team')).toBeInTheDocument()
    expect(screen.queryByText('customer_support_team')).not.toBeInTheDocument()
  })

  it('falls back to the internal technical name when no team display_name is set yet', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ specification_json: { name: 'my-team', agents: [], teams: [] } })] })

    renderPage()

    expect(await screen.findByText('my-team')).toBeInTheDocument()
  })
})

describe('SessionsPage status grouping', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it('groups sessions into a Live section and one In Progress section covering Spec/Solution/Testing', async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [
        session({ id: 's1', status: 'spec', specification_json: { name: 'spec-team', agents: [], teams: [] } }),
        session({ id: 's2', status: 'deployed', specification_json: { name: 'deployed-team', agents: [], teams: [] } }),
        session({ id: 's3', status: 'testing', specification_json: { name: 'testing-team', agents: [], teams: [] } }),
        session({ id: 's4', status: 'solution', specification_json: { name: 'solution-team', agents: [], teams: [] } }),
      ],
    })

    renderPage()
    await screen.findByText('deployed-team')

    const sectionHeadings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(sectionHeadings).toEqual(['Live (1)', 'In Progress (3)'])
  })

  it('omits section headers for statuses with no sessions', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ status: 'deployed' })] })

    renderPage()
    await screen.findByText('my-team')

    const sectionHeadings = screen.getAllByRole('heading', { level: 2 })
    expect(sectionHeadings).toHaveLength(1)
    expect(sectionHeadings[0]).toHaveTextContent('Live (1)')
  })
})

describe('SessionsPage status explanations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it('clicking the status help button toggles its explanation open and closed', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ status: 'deployed' })] })

    renderPage()
    await screen.findByText('my-team')

    const helpButton = screen.getByRole('button', { name: /what does live mean/i })
    expect(screen.queryByText(/ready for your organization/i)).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(helpButton)
    })
    expect(screen.getByText(/ready for your organization/i)).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(helpButton)
    })
    expect(screen.queryByText(/ready for your organization/i)).not.toBeInTheDocument()
  })

  it('shows a distinct explanation for the In Progress bucket', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ status: 'spec' })] })

    renderPage()
    await screen.findByText('my-team')

    const helpButton = screen.getByRole('button', { name: /what does in progress mean/i })
    await act(async () => {
      fireEvent.click(helpButton)
    })
    expect(screen.getByText(/still being built/i)).toBeInTheDocument()
  })
})

describe('SessionsPage draft deletion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it('shows a Delete button for a session that was never deployed', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ pipeline_id: null })] })

    renderPage()

    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('renders the delete action as an icon-only trash button, not a visible "Delete" label', async () => {
    // "Delete" as visible text reads as if the AI team itself is being
    // erased; a recycle-bin icon (with "Delete" kept as the accessible
    // name, for screen readers and the existing role queries) softens that.
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ pipeline_id: null })] })

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    expect(deleteButton).not.toHaveTextContent('Delete')
    expect(deleteButton.querySelector('svg')).toBeInTheDocument()
  })

  it('does not show a Delete button for a session linked to a live team', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ pipeline_id: 7 })] })

    renderPage()

    await screen.findByText('my-team')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('does nothing if the user cancels the confirmation', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ pipeline_id: null })] })
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(mockedApi.deleteSession).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('deletes the session and removes its card when confirmed', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ id: 's1', pipeline_id: null })] })
    mockedApi.deleteSession.mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPage()
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })

    await act(async () => {
      fireEvent.click(deleteButton)
    })

    expect(mockedApi.deleteSession).toHaveBeenCalledWith('s1')
    await waitFor(() => expect(screen.queryByText('my-team')).not.toBeInTheDocument())
  })

  it('shows an error banner and keeps the card if deletion fails', async () => {
    mockedApi.listSessions.mockResolvedValue({ sessions: [session({ pipeline_id: null })] })
    mockedApi.deleteSession.mockRejectedValue(new Error("Can't delete right now"))
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

describe('SessionsPage session-less deployed pipelines', () => {
  // A pipeline can be deployed straight through the admin Advanced/CRUD
  // page, bypassing the wizard entirely; the backend represents it as a
  // session-shaped entry with id: null (see builder.py's
  // _synthetic_session_for_pipeline) so My Teams shows every deployed team,
  // not just wizard-built ones.
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailTrigger.mockResolvedValue({ enabled: false, pipeline_name: null, status: 'disabled', daily_cap: 0 })
  })

  it('shows a card for a deployed pipeline with no builder session', async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [session({ id: null, pipeline_id: 3, specification_json: { name: 'orphan_team', agents: [], teams: [] } })],
    })

    renderPage()

    expect(await screen.findByText('orphan_team')).toBeInTheDocument()
  })

  it('does not show a Delete button for a session-less deployed pipeline', async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [session({ id: null, pipeline_id: 3, specification_json: { name: 'orphan_team', agents: [], teams: [] } })],
    })

    renderPage()

    await screen.findByText('orphan_team')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('clicking a session-less deployed pipeline card goes to Run a Team pre-selected, not a wizard page', async () => {
    mockedApi.listSessions.mockResolvedValue({
      sessions: [session({ id: null, pipeline_id: 3, specification_json: { name: 'orphan_team', agents: [], teams: [] } })],
    })

    renderPage()
    const card = await screen.findByText('orphan_team')

    await act(async () => {
      fireEvent.click(card)
    })

    expect(mockNavigate).toHaveBeenCalledWith('/run?pipeline=orphan_team')
  })
})
