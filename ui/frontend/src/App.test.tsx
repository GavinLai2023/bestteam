import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { RequireOrgMember, RequireAdmin } from './App'
import { useMe } from './lib/useMe'

vi.mock('./lib/useMe', () => ({ useMe: vi.fn() }))

// Guards are rendered around stub route elements (not the real pages, which
// fetch) so the tests exercise only the guard's redirect decision.
function renderOrgMemberGuard(entry = '/') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<RequireOrgMember />}>
          <Route path="/" element={<div>customer-home</div>} />
        </Route>
        <Route path="/advanced" element={<div>advanced-home</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderAdminGuard(entry = '/advanced') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<RequireAdmin />}>
          <Route path="/advanced" element={<div>advanced-home</div>} />
        </Route>
        <Route path="/" element={<div>customer-home</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('RequireOrgMember', () => {
  it('redirects a platform operator to /advanced', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: true, username: 'x', org: null }, loading: false, isAdmin: true })
    renderOrgMemberGuard('/')
    expect(screen.getByText('advanced-home')).toBeInTheDocument()
    expect(screen.queryByText('customer-home')).not.toBeInTheDocument()
  })

  it('renders the customer route for an org member', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderOrgMemberGuard('/')
    expect(screen.getByText('customer-home')).toBeInTheDocument()
    expect(screen.queryByText('advanced-home')).not.toBeInTheDocument()
  })

  it('renders nothing while the role is still loading', () => {
    vi.mocked(useMe).mockReturnValue({ me: null, loading: true, isAdmin: false })
    renderOrgMemberGuard('/')
    expect(screen.queryByText('customer-home')).not.toBeInTheDocument()
    expect(screen.queryByText('advanced-home')).not.toBeInTheDocument()
  })
})

describe('RequireAdmin', () => {
  it('renders the admin route for a platform operator', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: true, username: 'x', org: null }, loading: false, isAdmin: true })
    renderAdminGuard('/advanced')
    expect(screen.getByText('advanced-home')).toBeInTheDocument()
    expect(screen.queryByText('customer-home')).not.toBeInTheDocument()
  })

  it('redirects an org member away from an admin route to /', () => {
    vi.mocked(useMe).mockReturnValue({ me: { is_admin: false, username: 'x', org: 'acme' }, loading: false, isAdmin: false })
    renderAdminGuard('/advanced')
    expect(screen.getByText('customer-home')).toBeInTheDocument()
    expect(screen.queryByText('advanced-home')).not.toBeInTheDocument()
  })
})
