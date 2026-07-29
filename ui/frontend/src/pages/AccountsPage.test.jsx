import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import AccountsPage from './AccountsPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    adminOrgs: vi.fn(),
    adminUsers: vi.fn(),
    createAdminOrg: vi.fn(),
    setOrgActive: vi.fn(),
    createAdminUser: vi.fn(),
    resetAdminUserPassword: vi.fn(),
    moveAdminUser: vi.fn(),
    deleteAdminUser: vi.fn(),
  },
}))

const ORGS = [
  { name: 'acme', display_name: 'Acme Corp', active: true, member: 'alice' },
  { name: 'beta', display_name: '', active: false, member: null },
]
const USERS = [
  { username: 'alice', org: 'acme', is_admin: false },
  { username: 'op', org: null, is_admin: true },
]

beforeEach(() => {
  vi.clearAllMocks()
  api.adminOrgs.mockResolvedValue(ORGS)
  api.adminUsers.mockResolvedValue(USERS)
  api.createAdminOrg.mockResolvedValue({})
  api.setOrgActive.mockResolvedValue({})
  api.createAdminUser.mockResolvedValue({})
  api.deleteAdminUser.mockResolvedValue(null)
})

describe('AccountsPage', () => {
  it('renders orgs with status + member, and platform accounts read-only', async () => {
    render(<AccountsPage />)
    expect(await screen.findByText('Acme Corp')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(screen.getByText(/Deactivated/i)).toBeInTheDocument()
    expect(screen.getByText('op')).toBeInTheDocument()
    expect(screen.getByText(/managed via the CLI/i)).toBeInTheDocument()
  })

  it('creates an organization', async () => {
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.change(screen.getByLabelText(/organization name/i), { target: { value: 'gamma' } })
    fireEvent.click(screen.getByRole('button', { name: /create organization/i }))
    await waitFor(() => expect(api.createAdminOrg).toHaveBeenCalledWith('gamma', ''))
  })

  it('creates a user for an org with no member', async () => {
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.change(screen.getByLabelText('Username for beta'), { target: { value: 'bob' } })
    fireEvent.change(screen.getByLabelText('Password for beta'), { target: { value: 'pw' } })
    fireEvent.change(screen.getByLabelText('Confirm password for beta'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: /create user/i }))
    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledWith('bob', 'beta', 'pw'))
  })

  it('does not create a user when the passwords do not match', async () => {
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.change(screen.getByLabelText('Username for beta'), { target: { value: 'bob' } })
    fireEvent.change(screen.getByLabelText('Password for beta'), { target: { value: 'pw' } })
    fireEvent.change(screen.getByLabelText('Confirm password for beta'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByRole('button', { name: /create user/i }))
    expect(await screen.findByText(/passwords do not match/i)).toBeInTheDocument()
    expect(api.createAdminUser).not.toHaveBeenCalled()
  })

  it('keeps the entered values when creation fails', async () => {
    api.createAdminOrg.mockRejectedValue(new Error('boom'))
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.change(screen.getByLabelText(/organization name/i), { target: { value: 'gamma' } })
    fireEvent.click(screen.getByRole('button', { name: /create organization/i }))
    expect(await screen.findByText('boom')).toBeInTheDocument()
    expect(screen.getByLabelText(/organization name/i)).toHaveValue('gamma')
  })

  it('clears the form and warns (not fails) when creation succeeds but refresh fails', async () => {
    api.createAdminOrg.mockResolvedValue({})
    // mount reload succeeds; the post-create reload fails
    api.adminOrgs.mockResolvedValueOnce(ORGS).mockRejectedValueOnce(new Error('net'))
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.change(screen.getByLabelText(/organization name/i), { target: { value: 'gamma' } })
    fireEvent.click(screen.getByRole('button', { name: /create organization/i }))
    expect(await screen.findByText(/could not be refreshed/i)).toBeInTheDocument()
    // form cleared despite the refresh failure, so no duplicate retry
    expect(screen.getByLabelText(/organization name/i)).toHaveValue('')
  })

  it('deactivates an active org after confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.click(screen.getByRole('button', { name: /deactivate/i }))
    await waitFor(() => expect(api.setOrgActive).toHaveBeenCalledWith('acme', false))
  })

  it('deletes an org member after confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<AccountsPage />)
    await screen.findByText('Acme Corp')
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))
    await waitFor(() => expect(api.deleteAdminUser).toHaveBeenCalledWith('alice'))
  })
})
