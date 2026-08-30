import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import EmailTriggerToggle from './EmailTriggerToggle'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getEmailTrigger: vi.fn(),
    setEmailTrigger: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const trigger = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  pipeline_name: null,
  status: 'off',
  runs_today: 0,
  daily_cap: 50,
  last_checked_at: null,
  last_error: null,
  ...over,
})

describe('EmailTriggerToggle one automatic team per organisation', () => {
  beforeEach(() => vi.clearAllMocks())

  it('warns that turning this on stops the team that already runs automatically', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue(
      trigger({ enabled: true, pipeline_name: 'Support inbox', status: 'active' }) as never,
    )
    render(<EmailTriggerToggle pipelineName="Billing inbox" />)

    const notice = await screen.findByText(/only one team/i)
    expect(notice).toHaveTextContent(/Support inbox/)
  })

  it('says nothing about other teams when no team runs automatically yet', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue(trigger() as never)
    render(<EmailTriggerToggle pipelineName="Billing inbox" />)

    await screen.findByRole('button', { name: /run automatically/i })
    expect(screen.queryByText(/only one team/i)).not.toBeInTheDocument()
  })

  it('says nothing about other teams when this team is the one running automatically', async () => {
    mockedApi.getEmailTrigger.mockResolvedValue(
      trigger({ enabled: true, pipeline_name: 'Billing inbox', status: 'active' }) as never,
    )
    render(<EmailTriggerToggle pipelineName="Billing inbox" />)

    await screen.findByRole('button', { name: /turn off automatic runs/i })
    expect(screen.queryByText(/only one team/i)).not.toBeInTheDocument()
  })
})
