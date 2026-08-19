import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import LandingPage from './LandingPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: { listPipelines: vi.fn() },
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
  it('sends a brand-new org with no deployed pipelines to the wizard', async () => {
    mockedApi.listPipelines.mockResolvedValue({ pipelines: [] })
    renderLanding()
    expect(await screen.findByText('wizard-home')).toBeInTheDocument()
  })

  it('sends an org with at least one deployed pipeline to the activity dashboard', async () => {
    mockedApi.listPipelines.mockResolvedValue({ pipelines: ['support_team'] })
    renderLanding()
    expect(await screen.findByText('activity-home')).toBeInTheDocument()
  })

  it('falls back to the activity dashboard if the pipeline check fails', async () => {
    mockedApi.listPipelines.mockRejectedValue(new Error('boom'))
    renderLanding()
    expect(await screen.findByText('activity-home')).toBeInTheDocument()
  })
})
