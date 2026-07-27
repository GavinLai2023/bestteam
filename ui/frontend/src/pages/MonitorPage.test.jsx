import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import MonitorPage from './MonitorPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  API_BASE: 'http://127.0.0.1:8000',
  WS_BASE: 'ws://127.0.0.1:8000',
  api: { listWorkflows: vi.fn() },
}))

const renderPage = () =>
  render(
    <MemoryRouter>
      <MonitorPage />
    </MemoryRouter>,
  )

describe('MonitorPage backend error handling', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the server error detail, not "unreachable", when the backend returns an HTTP error', async () => {
    const err = new Error('Platform operators do not belong to an organization')
    err.status = 403
    api.listWorkflows.mockRejectedValue(err)

    renderPage()

    expect(
      await screen.findByText(/Platform operators do not belong to an organization/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Can't reach the backend/)).not.toBeInTheDocument()
  })

  it('shows "Can\'t reach the backend" on a genuine network failure (no HTTP status)', async () => {
    api.listWorkflows.mockRejectedValue(new TypeError('Failed to fetch'))

    renderPage()

    expect(await screen.findByText(/Can't reach the backend/)).toBeInTheDocument()
  })
})
