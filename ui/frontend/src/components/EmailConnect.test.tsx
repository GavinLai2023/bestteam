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
