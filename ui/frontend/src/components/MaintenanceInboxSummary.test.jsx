import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MaintenanceInboxSummary from './MaintenanceInboxSummary'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    automationResultsSummary: vi.fn(),
  },
}))

describe('MaintenanceInboxSummary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows a light empty state when the org uses this template but nothing was processed today', async () => {
    api.automationResultsSummary.mockResolvedValue({
      ever_used: true, emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })

    render(<MaintenanceInboxSummary />)

    expect(await screen.findByText(/no maintenance emails processed yet today/i)).toBeInTheDocument()
  })

  it('shows the counters when the org has results today', async () => {
    api.automationResultsSummary.mockResolvedValue({
      ever_used: true, emails_read: 5, maintenance_related: 3, drafts_created: 2,
      needs_attention: 1, possible_emergency: 1, skipped_non_maintenance: 2, errors: 0,
    })

    render(<MaintenanceInboxSummary />)

    expect(await screen.findByText('Emails read')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
  })

  it('renders nothing for an org that has never used this template', async () => {
    api.automationResultsSummary.mockResolvedValue({
      ever_used: false, emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })

    const { container } = render(<MaintenanceInboxSummary />)

    await vi.waitFor(() => expect(api.automationResultsSummary).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing on a fetch failure rather than blocking the page', async () => {
    api.automationResultsSummary.mockRejectedValue(new Error('boom'))

    const { container } = render(<MaintenanceInboxSummary />)

    await vi.waitFor(() => expect(api.automationResultsSummary).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it("requests the browser's local date, not the backend's UTC default", async () => {
    // The backend defaults to UTC "today" with no date param, which is the
    // wrong calendar day around local midnight for an org far from UTC
    // (Codex review finding) -- the frontend must pass its own local date.
    api.automationResultsSummary.mockResolvedValue({
      ever_used: true, emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })

    render(<MaintenanceInboxSummary />)

    await vi.waitFor(() => expect(api.automationResultsSummary).toHaveBeenCalled())
    const [dateArg] = api.automationResultsSummary.mock.calls[0]
    expect(dateArg).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    const now = new Date()
    const expected = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
    expect(dateArg).toBe(expected)
  })

  it('refreshes on the same 30s cadence as the adjacent needs-attention list, so new results while the page stays open are reflected (Codex review finding)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.automationResultsSummary.mockResolvedValue({
      ever_used: true, emails_read: 0, maintenance_related: 0, drafts_created: 0,
      needs_attention: 0, possible_emergency: 0, skipped_non_maintenance: 0, errors: 0,
    })

    render(<MaintenanceInboxSummary />)
    await vi.waitFor(() => expect(api.automationResultsSummary).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(30_000)
    expect(api.automationResultsSummary).toHaveBeenCalledTimes(2)
  })
})
