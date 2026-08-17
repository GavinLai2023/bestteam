import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import NotificationsPanel from './NotificationsPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    listNotifications: vi.fn(),
    markNotificationRead: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const notification = (over = {}) => ({
  id: 1,
  kind: 'trigger_health',
  severity: 'error' as const,
  title: 'Automatic email replies are failing',
  body: 'The last 3 automatic runs failed.',
  fingerprint: 'workflow',
  created_at: '2026-08-17T10:00:00+00:00',
  read: false,
  delivery_state: 'skipped' as const,
  ...over,
})

describe('NotificationsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listNotifications.mockResolvedValue({ notifications: [], unread: 0 })
  })

  it('says the system is healthy when there is nothing to report', async () => {
    render(<NotificationsPanel />)
    expect(await screen.findByText(/automatic email runs are healthy/i)).toBeInTheDocument()
  })

  it('renders a notification with its severity and body', async () => {
    mockedApi.listNotifications.mockResolvedValue({
      notifications: [notification()],
      unread: 1,
    })
    render(<NotificationsPanel />)
    expect(await screen.findByText(/automatic email replies are failing/i)).toBeInTheDocument()
    expect(screen.getByText(/the last 3 automatic runs failed/i)).toBeInTheDocument()
    expect(screen.getByText('Problem')).toBeInTheDocument()
  })

  it('reports the unread count to its parent', async () => {
    mockedApi.listNotifications.mockResolvedValue({
      notifications: [notification()],
      unread: 4,
    })
    const onUnreadChange = vi.fn()
    render(<NotificationsPanel onUnreadChange={onUnreadChange} />)
    await waitFor(() => expect(onUnreadChange).toHaveBeenCalledWith(4))
  })

  it('marks a notification read and updates the count', async () => {
    mockedApi.listNotifications.mockResolvedValue({
      notifications: [notification()],
      unread: 1,
    })
    mockedApi.markNotificationRead.mockResolvedValue({ ok: true, unread: 0 })
    const onUnreadChange = vi.fn()
    render(<NotificationsPanel onUnreadChange={onUnreadChange} />)

    fireEvent.click(await screen.findByRole('button', { name: /mark as read/i }))

    await waitFor(() => expect(mockedApi.markNotificationRead).toHaveBeenCalledWith(1))
    await waitFor(() => expect(onUnreadChange).toHaveBeenLastCalledWith(0))
    // The control disappears once it's read -- nothing left to do.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /mark as read/i })).not.toBeInTheDocument(),
    )
  })

  it('warns only when the customer own webhook failed, not when none is set', async () => {
    mockedApi.listNotifications.mockResolvedValue({
      notifications: [
        notification({ id: 1, delivery_state: 'skipped' }),
        notification({ id: 2, delivery_state: 'failed', title: 'Your mailbox cannot be reached' }),
      ],
      unread: 2,
    })
    render(<NotificationsPanel />)
    await screen.findByText(/your mailbox cannot be reached/i)
    // "skipped" just means in-app only, which is a normal configuration.
    expect(screen.getAllByText(/couldn't reach your webhook/i)).toHaveLength(1)
  })

  it('refetches with the unread filter when it is ticked', async () => {
    render(<NotificationsPanel />)
    await screen.findByText(/automatic email runs are healthy/i)
    fireEvent.click(screen.getByLabelText(/unread only/i))
    await waitFor(() => expect(mockedApi.listNotifications).toHaveBeenLastCalledWith(true))
  })
})
