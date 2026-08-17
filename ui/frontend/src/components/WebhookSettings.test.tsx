import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import WebhookSettings from './WebhookSettings'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    getNotificationSettings: vi.fn(),
    setNotificationSettings: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

describe('WebhookSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getNotificationSettings.mockResolvedValue({
      webhook_url: null,
      has_webhook_secret: false,
      enabled: true,
    })
    mockedApi.setNotificationSettings.mockResolvedValue({
      webhook_url: 'https://hooks.example.com/x',
      has_webhook_secret: false,
      enabled: true,
    })
  })

  it('says plainly that email content is never sent', async () => {
    render(<WebhookSettings />)
    expect(await screen.findByText(/never the contents of your email/i)).toBeInTheDocument()
  })

  it('saves a webhook address', async () => {
    render(<WebhookSettings />)
    fireEvent.change(await screen.findByLabelText(/webhook address/i), {
      target: { value: 'https://hooks.example.com/x' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setNotificationSettings).toHaveBeenCalledWith({
        webhook_url: 'https://hooks.example.com/x',
        enabled: true,
      }),
    )
  })

  it('omits the secret when it was not touched, so the stored one survives', async () => {
    mockedApi.getNotificationSettings.mockResolvedValue({
      webhook_url: 'https://hooks.example.com/x',
      has_webhook_secret: true,
      enabled: true,
    })
    render(<WebhookSettings />)
    await screen.findByLabelText(/webhook address/i)
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(mockedApi.setNotificationSettings).toHaveBeenCalled())
    const payload = mockedApi.setNotificationSettings.mock.calls[0][0]
    expect('webhook_secret' in payload).toBe(false)
  })

  it('sends the secret when the admin types one', async () => {
    render(<WebhookSettings />)
    fireEvent.change(await screen.findByLabelText(/signing secret/i), {
      target: { value: 'shh' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setNotificationSettings).toHaveBeenCalledWith(
        expect.objectContaining({ webhook_secret: 'shh' }),
      ),
    )
  })

  it('shows the API error instead of pretending it saved', async () => {
    mockedApi.setNotificationSettings.mockRejectedValue(
      new Error('The webhook URL must start with https://.'),
    )
    render(<WebhookSettings />)
    fireEvent.change(await screen.findByLabelText(/webhook address/i), {
      target: { value: 'http://insecure.example.com/x' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    expect(await screen.findByText(/must start with https/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Saved\.$/)).not.toBeInTheDocument()
  })

  it('can remove a stored signing secret', async () => {
    // A blank box means "leave it alone", so without this there was no way at
    // all to go back to unsigned delivery: the API takes an explicit empty
    // string and nothing could send one (Codex review finding).
    mockedApi.getNotificationSettings.mockResolvedValue({
      webhook_url: 'https://hooks.example.com/x',
      has_webhook_secret: true,
      enabled: true,
    })
    render(<WebhookSettings />)
    fireEvent.click(await screen.findByLabelText(/remove the stored secret/i))
    fireEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(mockedApi.setNotificationSettings).toHaveBeenCalledWith(
        expect.objectContaining({ webhook_secret: '' }),
      ),
    )
  })

  it('offers no removal checkbox when no secret is stored', async () => {
    render(<WebhookSettings />)
    await screen.findByLabelText(/signing secret/i)
    expect(screen.queryByLabelText(/remove the stored secret/i)).not.toBeInTheDocument()
  })

  it('tells the admin a secret is already stored without revealing it', async () => {
    mockedApi.getNotificationSettings.mockResolvedValue({
      webhook_url: 'https://hooks.example.com/x',
      has_webhook_secret: true,
      enabled: true,
    })
    render(<WebhookSettings />)
    const field = (await screen.findByLabelText(/signing secret/i)) as HTMLInputElement
    expect(field.value).toBe('')
    expect(screen.getByLabelText(/already set/i)).toBeInTheDocument()
  })
})
