import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
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
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: true, username: 'x', org: null }, loading: false, isAdmin: true })
    renderLayout()
    for (const label of ADMIN_LINKS) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    for (const label of CUSTOMER_LINKS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
  })

  it('shows only the customer links for an org member', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    for (const label of CUSTOMER_LINKS) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    for (const label of ADMIN_LINKS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
  })
})

describe('Layout scroll restoration', () => {
  it('scrolls to the top when navigating to a new route', async () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    const scrollToSpy = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})

    render(
      <MemoryRouter initialEntries={['/a']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/a" element={<Link to="/b">go to b</Link>} />
            <Route path="/b" element={<div>page-b</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    scrollToSpy.mockClear()
    fireEvent.click(screen.getByText('go to b'))

    expect(await screen.findByText('page-b')).toBeInTheDocument()
    expect(scrollToSpy).toHaveBeenCalledWith(0, 0)
  })
})
