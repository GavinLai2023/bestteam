import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import EmailBudgetSettings from './EmailBudgetSettings'
import { api } from '../lib/api'
import { parseCap } from '../lib/budgetCaps'
import type { EmailBudget } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    getEmailBudget: vi.fn(),
    setEmailBudget: vi.fn(),
  },
}))

// Spied, not replaced: every test below wants the real reading of the boxes.
// The one exception needs `parseCap` to report "not a number", which a
// `type="number"` input cannot produce -- see budgetCaps.test.ts.
vi.mock('../lib/budgetCaps', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/budgetCaps')>()
  return { parseCap: vi.fn(actual.parseCap) }
})

const mockedApi = vi.mocked(api)

const BUDGET: EmailBudget = {
  daily_message_cap: 25,
  monthly_cost_cap: 50,
  messages_today: 12,
  spent_this_month: 1.2345,
  unpriced_runs_this_month: 0,
  unpriced_models: [],
}

const budget = (overrides: Partial<EmailBudget> = {}): EmailBudget => ({ ...BUDGET, ...overrides })

describe('EmailBudgetSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailBudget.mockResolvedValue(budget())
    mockedApi.setEmailBudget.mockResolvedValue(budget())
  })

  it('shows usage against the daily message cap', async () => {
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/12 of 25/)).toBeInTheDocument()
  })

  // The model actually used, and exactly how much has been spent, are
  // admin-only information -- this page is reachable by any org member, not
  // just whoever manages billing (bug report: both leaked into this panel).
  it('never shows a specific amount spent, in the summary or the blind-spot notes', async () => {
    mockedApi.getEmailBudget.mockResolvedValue(
      budget({
        spent_this_month: 1.2345,
        monthly_cost_cap: 50,
        unpriced_runs_this_month: 3,
      }),
    )
    render(<EmailBudgetSettings />)

    await screen.findByText(/12 of 25/)
    // "(US$)" on the cap-setting label is fine -- it's the org choosing its
    // own limit, not a report of what has been spent.
    expect(screen.queryByText(/\$\d/)).not.toBeInTheDocument()
    expect(screen.queryByText(/spent/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/runs this month/i)).not.toBeInTheDocument()
  })

  it('never names the model behind an unpriced run', async () => {
    mockedApi.getEmailBudget.mockResolvedValue(
      budget({ unpriced_models: ['deepseek:deepseek-v4-pro'] }),
    )
    render(<EmailBudgetSettings />)

    await screen.findByText(/12 of 25/)
    expect(screen.queryByText(/deepseek/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/does not cover/i)).not.toBeInTheDocument()
  })

  it('says a cap is not set rather than implying one', async () => {
    mockedApi.getEmailBudget.mockResolvedValue(
      budget({ daily_message_cap: null, monthly_cost_cap: null }),
    )
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/12 \(no cap set\)/i)).toBeInTheDocument()
    expect((screen.getByLabelText(/messages a day/i) as HTMLInputElement).value).toBe('')
  })

  it('saves both caps', async () => {
    render(<EmailBudgetSettings />)

    fireEvent.change(await screen.findByLabelText(/messages a day/i), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText(/spend in a month/i), { target: { value: '12.5' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setEmailBudget).toHaveBeenCalledWith({
        daily_message_cap: 40,
        monthly_cost_cap: 12.5,
      }),
    )
  })

  it('clearing a field sends null, not zero', async () => {
    // 0 would be a cap of zero -- automation off -- which is not what an empty
    // box means.
    render(<EmailBudgetSettings />)

    fireEvent.change(await screen.findByLabelText(/messages a day/i), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText(/spend in a month/i), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(mockedApi.setEmailBudget).toHaveBeenCalled())
    expect(mockedApi.setEmailBudget).toHaveBeenCalledWith({
      daily_message_cap: null,
      monthly_cost_cap: null,
    })
  })

  it('shows the API error instead of pretending it saved', async () => {
    mockedApi.setEmailBudget.mockRejectedValue(
      new Error('daily_message_cap: Input should be greater than or equal to 1'),
    )
    render(<EmailBudgetSettings />)

    fireEvent.click(await screen.findByRole('button', { name: /save/i }))

    expect(await screen.findByText(/greater than or equal to 1/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Saved\.$/)).not.toBeInTheDocument()
  })

  it('refuses to save a figure it cannot read, rather than removing the cap', async () => {
    // A cap it cannot read must not reach the API as null: `JSON.stringify`
    // turns NaN into null, and null means "no limit", so an unreadable figure
    // would silently delete a customer's spend limit.
    vi.mocked(parseCap).mockReturnValueOnce(undefined)
    render(<EmailBudgetSettings />)

    fireEvent.click(await screen.findByRole('button', { name: /save/i }))

    expect(await screen.findByText(/please enter a number/i)).toBeInTheDocument()
    expect(mockedApi.setEmailBudget).not.toHaveBeenCalled()
    expect(screen.queryByText(/^Saved\.$/)).not.toBeInTheDocument()
  })

  it('offers no form when the limits could not be loaded', async () => {
    // An empty box means "no limit", so an empty form plus a Save button would
    // let an admin remove real caps while believing there were none.
    mockedApi.getEmailBudget.mockRejectedValue(new Error('Service unavailable'))
    render(<EmailBudgetSettings />)

    expect(await screen.findByText('Service unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/messages a day/i)).not.toBeInTheDocument()
  })

  it('shows the usage the save returned, not the figures it was opened with', async () => {
    mockedApi.setEmailBudget.mockResolvedValue(budget({ daily_message_cap: 40 }))
    render(<EmailBudgetSettings />)

    fireEvent.change(await screen.findByLabelText(/messages a day/i), { target: { value: '40' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    expect(await screen.findByText(/12 of 40/)).toBeInTheDocument()
  })
})
