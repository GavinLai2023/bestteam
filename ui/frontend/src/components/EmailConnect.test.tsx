import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import EmailConnect from './EmailConnect'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getOrgEmail: vi.fn(),
    setOrgEmail: vi.fn(),
    testOrgEmail: vi.fn(),
    clearOrgEmail: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const chooseMicrosoft = () => fireEvent.click(screen.getByLabelText(/microsoft 365/i))

describe('EmailConnect', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getOrgEmail.mockResolvedValue({ connected: false })
    mockedApi.setOrgEmail.mockResolvedValue({ connected: true })
  })

  it('defaults to the standard IMAP form', async () => {
    render(<EmailConnect />)
    expect(await screen.findByLabelText(/imap server/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/directory \(tenant\) id/i)).not.toBeInTheDocument()
  })

  it('swaps in the Microsoft 365 fields and hides the server address', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()

    expect(screen.getByLabelText(/directory \(tenant\) id/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/application \(client\) id/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/client secret/i)).toBeInTheDocument()
    // The server address is fixed for Exchange Online, so asking for it would
    // only invite a wrong answer. Basic auth is gone, so no app password either.
    expect(screen.queryByLabelText(/imap server/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/app password/i)).not.toBeInTheDocument()
  })

  it('posts a Microsoft 365 body with no password field', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()

    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'support@acme.com' },
    })
    fireEvent.change(screen.getByLabelText(/directory \(tenant\) id/i), {
      target: { value: 'tenant-1' },
    })
    fireEvent.change(screen.getByLabelText(/application \(client\) id/i), {
      target: { value: 'client-1' },
    })
    fireEvent.change(screen.getByLabelText(/client secret/i), { target: { value: 'shh' } })
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      auth_type: 'microsoft_oauth',
      username: 'support@acme.com',
      oauth_tenant_id: 'tenant-1',
      oauth_client_id: 'client-1',
      client_secret: 'shh',
      password: null,
    })
  })

  it('still posts the plain IMAP body when the standard option is kept', async () => {
    render(<EmailConnect />)
    fireEvent.change(await screen.findByLabelText(/imap server/i), {
      target: { value: 'imap.example.com' },
    })
    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'me@example.com' },
    })
    fireEvent.change(screen.getByLabelText(/app password/i), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      auth_type: 'password',
      host: 'imap.example.com',
      password: 'pw',
      client_secret: null,
      oauth_tenant_id: null,
    })
  })

  it('pre-fills the Entra identifiers on reconnect but never a secret', async () => {
    mockedApi.getOrgEmail.mockResolvedValue({
      connected: true,
      host: 'outlook.office365.com',
      username: 'support@acme.com',
      port: 993,
      auth_type: 'microsoft_oauth',
      oauth_tenant_id: 'tenant-1',
      oauth_client_id: 'client-1',
    })
    render(<EmailConnect />)

    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))

    expect(screen.getByLabelText(/directory \(tenant\) id/i)).toHaveValue('tenant-1')
    expect(screen.getByLabelText(/application \(client\) id/i)).toHaveValue('client-1')
    expect(screen.getByLabelText(/client secret/i)).toHaveValue('')
  })
})

describe('EmailConnect secret expiry', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getOrgEmail.mockResolvedValue({ connected: false })
    mockedApi.setOrgEmail.mockResolvedValue({ connected: true })
  })

  const fillMicrosoft = () => {
    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'support@acme.com' },
    })
    fireEvent.change(screen.getByLabelText(/directory \(tenant\) id/i), {
      target: { value: 'tenant-1' },
    })
    fireEvent.change(screen.getByLabelText(/application \(client\) id/i), {
      target: { value: 'client-1' },
    })
    fireEvent.change(screen.getByLabelText(/client secret/i), { target: { value: 'shh' } })
  }

  it('offers the expiry date only for Microsoft 365', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    expect(screen.queryByLabelText(/secret expiry date/i)).not.toBeInTheDocument()
    chooseMicrosoft()
    expect(screen.getByLabelText(/secret expiry date/i)).toBeInTheDocument()
  })

  it('sends the expiry date when one is entered', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()
    fillMicrosoft()
    fireEvent.change(screen.getByLabelText(/secret expiry date/i), {
      target: { value: '2027-01-31' },
    })
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      oauth_secret_expires_at: '2027-01-31',
    })
  })

  it('leaves the expiry null when it is left blank, since it is optional', async () => {
    render(<EmailConnect />)
    await screen.findByLabelText(/imap server/i)
    chooseMicrosoft()
    fillMicrosoft()
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      oauth_secret_expires_at: null,
    })
  })

  it('never sends an expiry for a plain IMAP mailbox', async () => {
    render(<EmailConnect />)
    fireEvent.change(await screen.findByLabelText(/imap server/i), {
      target: { value: 'imap.example.com' },
    })
    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'me@example.com' },
    })
    fireEvent.change(screen.getByLabelText(/app password/i), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      oauth_secret_expires_at: null,
    })
  })
})

describe('EmailConnect one mailbox per organisation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.setOrgEmail.mockResolvedValue({ connected: true })
    mockedApi.getOrgEmail.mockResolvedValue({
      connected: true,
      host: 'imap.example.com',
      username: 'support@acme.com',
      port: 993,
      auth_type: 'password',
    })
  })

  it('says the connected mailbox is shared by every team in the organisation', async () => {
    render(<EmailConnect />)
    expect(await screen.findByText(/every team in your organisation/i)).toBeInTheDocument()
  })

  it('warns before saving when the address entered is a different mailbox', async () => {
    render(<EmailConnect />)
    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))

    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'billing@acme.com' },
    })

    const warning = screen.getByText(/switch every team over to billing@acme.com/i)
    expect(warning).toBeInTheDocument()
    expect(warning).toHaveTextContent(/automatic runs/i)
  })

  it('stays quiet while the same address is being reconnected', async () => {
    render(<EmailConnect />)
    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))

    expect(screen.queryByText(/switch every team over/i)).not.toBeInTheDocument()
  })
})

describe('EmailConnect switching to a different mailbox', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.setOrgEmail.mockResolvedValue({ connected: true })
    mockedApi.getOrgEmail.mockResolvedValue({
      connected: true,
      host: 'imap.example.com',
      username: 'support@acme.com',
      port: 993,
      auth_type: 'password',
    })
  })

  // The shared `answerConfirm` helper finds the dialog by the only "Cancel" on
  // screen; this form has one of its own while editing, so scope it here.
  const dialog = () => document.querySelector('.confirm-dialog')
  const answer = (accept: boolean) => {
    const buttons = dialog()!.querySelectorAll('button')
    fireEvent.click(accept ? buttons[buttons.length - 1] : buttons[0])
  }

  const reconnectAs = async (address: string) => {
    render(<EmailConnect />)
    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))
    fireEvent.change(screen.getByLabelText(/email address/i), { target: { value: address } })
    fireEvent.change(screen.getByLabelText(/app password/i), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: /connect mailbox/i }))
  }

  it('asks to confirm before switching every team to another address', async () => {
    await reconnectAs('billing@acme.com')

    await waitFor(() => expect(dialog()).not.toBeNull())
    expect(dialog()!.textContent).toMatch(/billing@acme\.com/)
    expect(dialog()!.textContent).toMatch(/automatic runs/i)
    expect(mockedApi.setOrgEmail).not.toHaveBeenCalled()
  })

  it('saves nothing when that confirmation is cancelled', async () => {
    await reconnectAs('billing@acme.com')
    await waitFor(() => expect(dialog()).not.toBeNull())

    answer(false)

    await waitFor(() => expect(dialog()).toBeNull())
    expect(mockedApi.setOrgEmail).not.toHaveBeenCalled()
  })

  it('saves the new mailbox once the switch is confirmed', async () => {
    await reconnectAs('billing@acme.com')
    await waitFor(() => expect(dialog()).not.toBeNull())

    answer(true)

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(mockedApi.setOrgEmail.mock.calls[0][0]).toMatchObject({
      username: 'billing@acme.com',
    })
  })

  it('does not ask when only the password is being rotated', async () => {
    await reconnectAs('support@acme.com')

    await waitFor(() => expect(mockedApi.setOrgEmail).toHaveBeenCalled())
    expect(dialog()).toBeNull()
  })
})
