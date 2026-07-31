import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Layout from './Layout'
import { useMe } from '../lib/useMe'

vi.mock('../lib/useMe', () => ({ useMe: vi.fn() }))

const renderLayout = () =>
  render(
    <MemoryRouter>
      <Layout />
    </MemoryRouter>,
  )

const CUSTOMER_LINKS = ['Build a team', 'My teams', 'Run a team', 'Activity']
const ADMIN_LINKS = ['Accounts', 'Advanced', 'Memory']

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Layout nav', () => {
  it('shows only the admin links for a platform operator', () => {
    useMe.mockReturnValue({ me: { is_admin: true }, loading: false, isAdmin: true })
    renderLayout()
    for (const label of ADMIN_LINKS) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    for (const label of CUSTOMER_LINKS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
  })

  it('shows only the customer links for an org member', () => {
    useMe.mockReturnValue({ me: { is_admin: false }, loading: false, isAdmin: false })
    renderLayout()
    for (const label of CUSTOMER_LINKS) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    for (const label of ADMIN_LINKS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
  })
})
