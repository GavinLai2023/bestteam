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

const CUSTOMER_LINKS = ['Dashboard', 'Build a team', 'My teams', 'Run a team']
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

  it('marks the product as beta beside the wordmark', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    expect(screen.getByText('beta')).toBeInTheDocument()
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

// Language, Change password and Log out are account settings, not navigation.
// Left in the nav row they sat between the links and each other -- eight items
// on one line, with a select box in the middle of them.
describe('Layout account menu', () => {
  const openMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Account' }))

  it('names the signed-in user on the trigger', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'ana', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    expect(screen.getByRole('button', { name: 'Account' })).toHaveTextContent('ana')
  })

  it('keeps the account items out of the nav row until it is opened', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    expect(screen.queryByRole('button', { name: 'Change password' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Language')).not.toBeInTheDocument()
  })

  it('reveals all three once opened', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    openMenu()
    expect(screen.getByRole('button', { name: 'Change password' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()
    expect(screen.getByLabelText('Language')).toBeInTheDocument()
  })

  it('closes on Escape', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    openMenu()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
  })

  it('closes when a click lands outside it', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    openMenu()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
  })
})

describe('Layout change-password entry point', () => {
  const openMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Account' }))

  it.each([
    ['a platform operator', { is_admin: true, username: 'x', org: null }, true],
    ['an org member', { is_admin: false, username: 'x', org: 'acme' }, false],
  ])('offers it to %s', (_who, me, isAdmin) => {
    vi.mocked(useMe).mockReturnValue({ me, loading: false, isAdmin })
    renderLayout()
    openMenu()
    expect(screen.getByRole('button', { name: 'Change password' })).toBeInTheDocument()
  })

  // tests/e2e/test_smoke.py opens the account menu, then clicks
  // `button.logout-button`; a second button wearing that class is a Playwright
  // strict-mode failure, not a style bug.
  it('leaves the log-out selector matching exactly one button', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    const { container } = renderLayout()
    openMenu()
    expect(container.querySelectorAll('button.logout-button')).toHaveLength(1)
  })

  it('opens the dialog, which stays closed until asked for', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderLayout()
    expect(screen.queryByLabelText(/current password/i)).not.toBeInTheDocument()

    openMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))

    expect(screen.getByLabelText(/current password/i)).toBeInTheDocument()
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
