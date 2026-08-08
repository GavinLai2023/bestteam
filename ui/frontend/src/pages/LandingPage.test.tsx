import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import LandingPage from './LandingPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: { listWorkflows: vi.fn() },
}))

const mockedApi = vi.mocked(api)

const renderLanding = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/wizard" element={<div>wizard-home</div>} />
        <Route path="/activity" element={<div>activity-home</div>} />
      </Routes>
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.clearAllMocks()
})

describe('LandingPage', () => {
  it('sends a brand-new org with no deployed workflows to the wizard', async () => {
    mockedApi.listWorkflows.mockResolvedValue({ workflows: [] })
    renderLanding()
    expect(await screen.findByText('wizard-home')).toBeInTheDocument()
  })

  it('sends an org with at least one deployed workflow to the activity dashboard', async () => {
    mockedApi.listWorkflows.mockResolvedValue({ workflows: ['support_team'] })
    renderLanding()
    expect(await screen.findByText('activity-home')).toBeInTheDocument()
  })

  it('falls back to the activity dashboard if the workflow check fails', async () => {
    mockedApi.listWorkflows.mockRejectedValue(new Error('boom'))
    renderLanding()
    expect(await screen.findByText('activity-home')).toBeInTheDocument()
  })
})
