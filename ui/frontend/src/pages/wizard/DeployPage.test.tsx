import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DeployPage from './DeployPage'
import { api } from '../../lib/api'
import type { BuilderSession } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: {
    getEmailTrigger: vi.fn(),
    setEmailTrigger: vi.fn(),
    getOrgEmail: vi.fn(),
    setOrgEmail: vi.fn(),
    testOrgEmail: vi.fn(),
    clearOrgEmail: vi.fn(),
    deploySession: vi.fn(),
    getEmailFilter: vi.fn(),
    setEmailFilter: vi.fn(),
    getEmailBudget: vi.fn(),
    setEmailBudget: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

let mockContext: {
  session: BuilderSession | null
  setSession: (session: BuilderSession) => void
  loading: boolean
  sessionId: string
}

// `vi.hoisted` because the `vi.mock` factory below is hoisted above this
// line and would otherwise read `navigate` before it is initialised.
const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useOutletContext: () => mockContext, useNavigate: () => navigate }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <DeployPage />
    </MemoryRouter>,
  )

const deployedSession = (): BuilderSession => ({
  id: 's1',
  status: 'deployed',
  intent_text: 'reply to emails',
  specification_json: { name: 'Automated_Email_Responder_Team', agents: [], teams: [] },
  uses_email: true,
  updated_at: '2026-08-06T00:00:00Z',
})

describe('DeployPage live automatic-runs mailbox connection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: deployedSession(), setSession: vi.fn(), loading: false, sessionId: 's1' }
    mockedApi.getEmailTrigger.mockResolvedValue({
      enabled: false,
      pipeline_name: null,
      status: 'off',
      daily_cap: 50,
    })
    mockedApi.getEmailFilter.mockResolvedValue({
      skip_bulk: true,
      sender_blocklist: [],
      sender_allowlist: [],
      subject_blocklist: [],
    })
    mockedApi.getEmailBudget.mockResolvedValue({
      daily_message_cap: null,
      monthly_cost_cap: null,
      messages_today: 0,
      spent_this_month: null,
      unpriced_runs_this_month: 0,
      unpriced_models: [],
    })
  })

  it('shows a way to connect the mailbox on the live/Go Live page when the team uses email but no mailbox is connected', async () => {
    mockedApi.getOrgEmail.mockResolvedValue({ connected: false })

    renderPage()

    // The user should be able to reach the mailbox-connect form directly from
    // this screen, not just see an opaque "connect your mailbox" error after
    // clicking the automatic-runs toggle.
    expect(await screen.findByText('Connect your mailbox')).toBeInTheDocument()
  })

  it("shows the email team's mail-filter and volume-limit settings on its own Deploy page, not on Activity", async () => {
    mockedApi.getOrgEmail.mockResolvedValue({ connected: true, host: 'imap.example.com', username: 'a@example.com' })

    renderPage()

    expect(await screen.findByText('Which mail to skip')).toBeInTheDocument()
    expect(screen.getByText('How much automatic work to allow')).toBeInTheDocument()
  })
})

describe('DeployPage launching', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const session = deployedSession()
    session.status = 'spec'
    session.uses_email = false
    mockContext = { session, setSession: vi.fn(), loading: false, sessionId: 's1' }
  })

  // The click that publishes a team is the one moment in the wizard worth
  // dressing up; nothing else on any step wears `btn-hero`.
  it('dresses the publish button as the hero', () => {
    renderPage()

    expect(screen.getByRole('button', { name: 'Launch my team' })).toHaveClass('btn-hero')
  })
})

describe('DeployPage next steps once the team is live', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockContext = { session: deployedSession(), setSession: vi.fn(), loading: false, sessionId: 's1' }
  })

  describe('a team that answers questions', () => {
    beforeEach(() => {
      mockContext.session!.uses_email = false
    })

    // Nothing is left to configure here, so the useful next move is to talk to
    // it -- with this team pre-selected, which is what the query string is for.
    it('offers a try-it-out shortcut into Run a team', () => {
      renderPage()

      fireEvent.click(screen.getByRole('button', { name: 'Try it out' }))

      expect(navigate).toHaveBeenCalledWith('/run?pipeline=Automated_Email_Responder_Team')
    })

    it('says nothing about automatic runs, which it does not have', () => {
      renderPage()

      expect(screen.queryByText(/switch on automatic runs/i)).not.toBeInTheDocument()
    })
  })

  describe('an email team', () => {
    beforeEach(() => {
      mockedApi.getOrgEmail.mockResolvedValue({ connected: true, host: 'imap.example.com', username: 'a@example.com' })
    })

    // Deployed but idle: the toggle above is off by default, so "live" on its
    // own would leave the customer waiting for mail nothing is collecting.
    it('says the automatic-runs switch is still to come', async () => {
      renderPage()

      expect(await screen.findByText(/switch on automatic runs/i)).toBeInTheDocument()
    })

    // Not "Try it out": the switch above is the real next step, so this button
    // only closes the build.
    it('closes the build on My teams instead', async () => {
      renderPage()

      fireEvent.click(await screen.findByRole('button', { name: 'Done' }))

      expect(navigate).toHaveBeenCalledWith('/teams')
      expect(screen.queryByRole('button', { name: 'Try it out' })).not.toBeInTheDocument()
    })
  })
})
