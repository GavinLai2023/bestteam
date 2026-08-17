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

  it('shows usage against each cap', async () => {
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/12 of 25/)).toBeInTheDocument()
    expect(screen.getByText(/\$1\.2345 of \$50\.00/)).toBeInTheDocument()
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

  it('warns when the cap cannot cover a model', async () => {
    mockedApi.getEmailBudget.mockResolvedValue(budget({ unpriced_models: ['acme:whizz-1'] }))
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/does not cover/i)).toBeInTheDocument()
    expect(screen.getByText(/acme:whizz-1/)).toBeInTheDocument()
  })

  it('says nothing about uncovered models when every model has a price', async () => {
    render(<EmailBudgetSettings />)

    await screen.findByText(/12 of 25/)
    expect(screen.queryByText(/does not cover/i)).not.toBeInTheDocument()
  })

  it('reports unpriced runs so the blind spot is visible', async () => {
    mockedApi.getEmailBudget.mockResolvedValue(budget({ unpriced_runs_this_month: 3 }))
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/3 runs this month/i)).toBeInTheDocument()
  })

  it('does not show an unmeasured month as a measured $0.00', async () => {
    // null is "nothing this month could be priced", which is not the same as
    // "this month cost nothing".
    mockedApi.getEmailBudget.mockResolvedValue(
      budget({ spent_this_month: null, unpriced_runs_this_month: 2 }),
    )
    render(<EmailBudgetSettings />)

    expect(await screen.findByText(/nothing this month has a price/i)).toBeInTheDocument()
    expect(screen.queryByText(/\$0\.00/)).not.toBeInTheDocument()
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
