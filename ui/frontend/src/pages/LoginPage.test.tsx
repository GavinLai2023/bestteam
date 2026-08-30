import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import LoginPage from './LoginPage'
import { api } from '../lib/api'
import { setLanguage } from '../lib/i18n'

// `importActual` so `TOKEN_KEY` stays the real constant -- a bare factory
// would make the page write to `localStorage[undefined]`.
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, api: { login: vi.fn() } }
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

afterEach(() => {
  setLanguage('en')
})

describe('LoginPage', () => {
  // tests/e2e/test_smoke.py drives the deployed page through exactly these
  // selectors; a redesign that renamed any of them would pass here and fail
  // in the e2e tier.
  it('keeps the ids and the submit button the e2e smoke test drives', () => {
    const { container } = renderPage()
    expect(container.querySelector('#username')).toBeInTheDocument()
    expect(container.querySelector('#password')).toBeInTheDocument()
    expect(container.querySelector('button[type=submit]')).toBeInTheDocument()
  })

  it('marks the product as beta beside the wordmark', () => {
    renderPage()
    expect(screen.getByText('beta')).toBeInTheDocument()
  })

  it('renders a failed login in a .banner-error, which e2e waits for', async () => {
    vi.mocked(api.login).mockRejectedValue(new Error('Invalid username or password'))
    const { container } = renderPage()

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: 'nope' } })
    fireEvent.click(container.querySelector('button[type=submit]')!)

    await waitFor(() => {
      expect(container.querySelector('.banner-error')).toHaveTextContent('Invalid username or password')
    })
  })

  it('stores the token on success', async () => {
    vi.mocked(api.login).mockResolvedValue({ access_token: 'tok-123' })
    const { container } = renderPage()

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: '  alice  ' } })
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: 'hunter2' } })
    fireEvent.click(container.querySelector('button[type=submit]')!)

    await waitFor(() => expect(localStorage.getItem('bestteam_token')).toBe('tok-123'))
    expect(api.login).toHaveBeenCalledWith('alice', 'hunter2')
  })

  it('translates the whole page, language control included', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: 'Log in' })).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: /language/i }), { target: { value: 'zh-CN' } })

    // The re-render rides an i18next event, so it lands after this tick.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument()
    })
    expect(screen.getByLabelText('用户名')).toBeInTheDocument()
    expect(screen.getByText('一句需求，一支团队')).toBeInTheDocument()
  })

  it('reveals and re-hides the password', () => {
    const { container } = renderPage()
    const field = container.querySelector('#password')!
    expect(field).toHaveAttribute('type', 'password')

    fireEvent.click(screen.getByRole('button', { name: /show password/i }))
    expect(field).toHaveAttribute('type', 'text')

    fireEvent.click(screen.getByRole('button', { name: /hide password/i }))
    expect(field).toHaveAttribute('type', 'password')
  })

  it('warns while Caps Lock is on and stops when it goes off', () => {
    const { container } = renderPage()
    const field = container.querySelector('#password')!

    // `getModifierState` is a method on the synthetic event, not a property
    // fireEvent can stub -- React delegates to the native one, so the state has
    // to be set through KeyboardEventInit.
    fireEvent.keyUp(field, { key: 'a', modifierCapsLock: true })
    expect(screen.getByText(/caps lock is on/i)).toBeInTheDocument()

    fireEvent.keyUp(field, { key: 'a', modifierCapsLock: false })
    expect(screen.queryByText(/caps lock is on/i)).not.toBeInTheDocument()
  })

  it('will not submit an empty form', () => {
    const { container } = renderPage()
    expect(container.querySelector('button[type=submit]')).toBeDisabled()
  })
})
