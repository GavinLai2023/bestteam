import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import NeedsAttentionList from './NeedsAttentionList'
import { api } from '../lib/api'
import type { AutomationResult } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    listAutomationResults: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const RESULT: AutomationResult = {
  id: 1,
  run_id: 'run-42',
  status: 'needs_attention',
  created_at: '2026-08-02T10:00:00Z',
  payload: {
    priority: 'possible_emergency',
    summary: 'Active leak under the kitchen sink.',
    extracted: { property_address: '12 Example St, Unit 3' },
    human_reason: 'Active leak; missing callback number.',
    action: { draft_created: true },
  },
}

describe('NeedsAttentionList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders nothing when there is nothing to review', async () => {
    mockedApi.listAutomationResults.mockResolvedValue({ results: [] })

    const { container } = render(<NeedsAttentionList />)

    await vi.waitFor(() => expect(mockedApi.listAutomationResults).toHaveBeenCalledWith({ needs_attention: true, limit: 20 }))
    expect(container).toBeEmptyDOMElement()
  })

  it('shows each item with priority, summary, address, and reason', async () => {
    mockedApi.listAutomationResults.mockResolvedValue({ results: [RESULT] })

    render(<NeedsAttentionList />)

    expect(await screen.findByText('Possible emergency')).toBeInTheDocument()
    expect(screen.getByText('Active leak under the kitchen sink.')).toBeInTheDocument()
    expect(screen.getByText('12 Example St, Unit 3')).toBeInTheDocument()
    expect(screen.getByText(/Active leak; missing callback number/)).toBeInTheDocument()
    expect(screen.getByText(/Draft created/)).toBeInTheDocument()
  })

  it('falls back to "Address not identified" when no address was extracted', async () => {
    mockedApi.listAutomationResults.mockResolvedValue({
      results: [{ ...RESULT, payload: { ...RESULT.payload, extracted: {} } }],
    })

    render(<NeedsAttentionList />)

    expect(await screen.findByText('Address not identified')).toBeInTheDocument()
  })

  it('clicking "View run" calls onOpenRun with the result\'s run_id', async () => {
    mockedApi.listAutomationResults.mockResolvedValue({ results: [RESULT] })
    const onOpenRun = vi.fn()

    render(<NeedsAttentionList onOpenRun={onOpenRun} />)

    const button = await screen.findByText('View run')
    await act(async () => {
      fireEvent.click(button)
    })

    expect(onOpenRun).toHaveBeenCalledWith('run-42')
  })

  it('shows an error banner when the list fails to load', async () => {
    mockedApi.listAutomationResults.mockRejectedValue(new Error('boom'))

    render(<NeedsAttentionList />)

    expect(await screen.findByText(/Couldn't load the needs-attention list/)).toBeInTheDocument()
  })
})
