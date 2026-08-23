import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import ChangePasswordDialog from './ChangePasswordDialog'
import { api } from '../lib/api'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, api: { changePassword: vi.fn() } }
})

const fill = (current: string, next: string, confirm: string) => {
  fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: current } })
  fireEvent.change(screen.getByLabelText(/^new password$/i), { target: { value: next } })
  fireEvent.change(screen.getByLabelText(/confirm new password/i), { target: { value: confirm } })
}

const submitButton = () => screen.getByRole('button', { name: /^change password$/i })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('ChangePasswordDialog', () => {
  it('renders nothing while closed', () => {
    const { container } = render(<ChangePasswordDialog open={false} onClose={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('blocks a submit until the two new passwords match', () => {
    render(<ChangePasswordDialog open onClose={vi.fn()} />)
    fill('hunter2', 'correct-horse', 'correct-hors')

    expect(screen.getByText(/do not match/i)).toBeInTheDocument()
    expect(submitButton()).toBeDisabled()
  })

  it('blocks a submit on a new password under eight characters', () => {
    render(<ChangePasswordDialog open onClose={vi.fn()} />)
    fill('hunter2', 'short7c', 'short7c')

    expect(submitButton()).toBeDisabled()
  })

  it('swaps in the fresh token and reports the other sessions ending', async () => {
    vi.mocked(api.changePassword).mockResolvedValue({ access_token: 'tok-new' })
    render(<ChangePasswordDialog open onClose={vi.fn()} />)
    fill('hunter2', 'correct-horse', 'correct-horse')

    fireEvent.click(submitButton())

    await waitFor(() => expect(screen.getByText(/signed out/i)).toBeInTheDocument())
    expect(api.changePassword).toHaveBeenCalledWith('hunter2', 'correct-horse')
    // Without this the very next request carries the token the change revoked.
    expect(localStorage.getItem('bestteam_token')).toBe('tok-new')
  })

  it('shows the backend error and keeps the form usable', async () => {
    vi.mocked(api.changePassword).mockRejectedValue(new Error('Current password is incorrect'))
    render(<ChangePasswordDialog open onClose={vi.fn()} />)
    fill('wrong', 'correct-horse', 'correct-horse')

    fireEvent.click(submitButton())

    await waitFor(() => {
      expect(screen.getByText('Current password is incorrect')).toBeInTheDocument()
    })
    expect(localStorage.getItem('bestteam_token')).toBeNull()
    expect(submitButton()).toBeEnabled()
  })

  it('clears what was typed when it closes', async () => {
    const onClose = vi.fn()
    render(<ChangePasswordDialog open onClose={onClose} />)
    fill('hunter2', 'correct-horse', 'correct-horse')

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))

    expect(onClose).toHaveBeenCalled()
    await waitFor(() => {
      expect(screen.getByLabelText(/current password/i)).toHaveValue('')
    })
  })
})
