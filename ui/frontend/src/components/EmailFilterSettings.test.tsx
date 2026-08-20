import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import EmailFilterSettings from './EmailFilterSettings'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getEmailFilter: vi.fn(),
    setEmailFilter: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const RULES = {
  skip_bulk: true,
  sender_blocklist: ['noreply@example.com'],
  sender_allowlist: [],
  subject_blocklist: ['out of office'],
}

describe('EmailFilterSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEmailFilter.mockResolvedValue({ ...RULES })
    mockedApi.setEmailFilter.mockResolvedValue({ ...RULES })
  })

  it('loads the current rules', async () => {
    render(<EmailFilterSettings />)

    const blocked = (await screen.findByLabelText(/never process mail from/i)) as HTMLTextAreaElement
    expect(blocked.value).toBe('noreply@example.com')
    expect((screen.getByLabelText(/skip bulk mail/i) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText(/only process mail from/i) as HTMLTextAreaElement).value).toBe('')
    expect(
      (screen.getByLabelText(/whose subject contains/i) as HTMLTextAreaElement).value,
    ).toBe('out of office')
  })

  it('saves edited rules', async () => {
    render(<EmailFilterSettings />)

    fireEvent.change(await screen.findByLabelText(/never process mail from/i), {
      target: { value: 'noreply@example.com\n*@spam.example' },
    })
    fireEvent.click(screen.getByLabelText(/skip bulk mail/i))
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setEmailFilter).toHaveBeenCalledWith({
        skip_bulk: false,
        sender_blocklist: ['noreply@example.com', '*@spam.example'],
        sender_allowlist: [],
        subject_blocklist: ['out of office'],
      }),
    )
  })

  it('parses one pattern per line, ignoring blank lines and stray spaces', async () => {
    render(<EmailFilterSettings />)

    fireEvent.change(await screen.findByLabelText(/only process mail from/i), {
      target: { value: '  a@example.com  \n\n*@partner.example\n' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setEmailFilter).toHaveBeenCalledWith(
        expect.objectContaining({ sender_allowlist: ['a@example.com', '*@partner.example'] }),
      ),
    )
  })

  it('points to the Automations tab for released mail, not "above" -- this panel no longer sits next to it', async () => {
    render(<EmailFilterSettings />)

    expect(await screen.findByText(/Team activity page.s Automations tab/i)).toBeInTheDocument()
  })

  it('says which two pattern forms are allowed', async () => {
    // The UI has to state this: there are no regular expressions anywhere in
    // this feature, and an admin who types one and sees nothing filtered has
    // no other way to find out why.
    //
    // Empty lists on purpose: React writes a textarea's value into its text
    // content, so a stored `noreply@example.com` rule would be a second match
    // for the assertions below and tell us nothing about the copy.
    mockedApi.getEmailFilter.mockResolvedValue({
      skip_bulk: true,
      sender_blocklist: [],
      sender_allowlist: [],
      subject_blocklist: [],
    })
    render(<EmailFilterSettings />)

    expect(await screen.findByText(/\*@example\.com/)).toBeInTheDocument()
    expect(screen.getByText(/noreply@example\.com/)).toBeInTheDocument()
    expect(screen.getByText(/no regular expressions/i)).toBeInTheDocument()
  })

  it('shows the API error instead of pretending it saved', async () => {
    mockedApi.setEmailFilter.mockRejectedValue(
      new Error('sender_blocklist: String should have at most 200 characters'),
    )
    render(<EmailFilterSettings />)

    fireEvent.click(await screen.findByRole('button', { name: /save/i }))

    expect(await screen.findByText(/at most 200 characters/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Saved\.$/)).not.toBeInTheDocument()
  })

  it('offers no form when the rules could not be loaded', async () => {
    // Empty boxes plus a Save button would let an admin replace real rules
    // with none while believing they had none to start with.
    mockedApi.getEmailFilter.mockRejectedValue(new Error('Service unavailable'))
    render(<EmailFilterSettings />)

    expect(await screen.findByText('Service unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/never process mail from/i)).not.toBeInTheDocument()
  })

  it('confirms a save so the admin knows the rules are live', async () => {
    render(<EmailFilterSettings />)

    fireEvent.click(await screen.findByRole('button', { name: /save/i }))

    expect(await screen.findByText(/^Saved\.$/)).toBeInTheDocument()
  })
})
