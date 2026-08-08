import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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
  },
}))

const mockedApi = vi.mocked(api)

let mockContext: {
  session: BuilderSession | null
  setSession: (session: BuilderSession) => void
  loading: boolean
  sessionId: string
}

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useOutletContext: () => mockContext, useNavigate: () => vi.fn() }
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
      workflow_name: null,
      status: 'off',
      daily_cap: 50,
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
})
