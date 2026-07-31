import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import MemoryPage from './MemoryPage'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: {
    memoryUsers: vi.fn(),
    memoryRecords: vi.fn(),
    deleteMemoryRecord: vi.fn(),
    clearUserMemory: vi.fn(),
  },
}))

const USERS = [
  { user_id: 'alice', org_id: 5, episodic: 1, semantic: 0, procedural: 0, total: 1 },
  { user_id: 'bob', org_id: null, episodic: 1, semantic: 0, procedural: 0, total: 1 },
]

beforeEach(() => {
  vi.clearAllMocks()
  api.memoryUsers.mockResolvedValue({ enabled: true, users: USERS })
  api.memoryRecords.mockResolvedValue({ enabled: true, records: [] })
})

describe('MemoryPage org scoping', () => {
  it('requests only legacy rows when a legacy (no org) identity is selected', async () => {
    // Finding 3: a legacy identity (org_id null) must send org='legacy', not omit
    // org -- otherwise the backend reads the username across every org.
    render(<MemoryPage />)
    fireEvent.click(await screen.findByRole('button', { name: /bob/ }))
    await waitFor(() =>
      expect(api.memoryRecords).toHaveBeenCalledWith(
        'bob',
        expect.objectContaining({ org: 'legacy' }),
      ),
    )
  })

  it('scopes to the concrete org for an org-bound identity', async () => {
    render(<MemoryPage />)
    fireEvent.click(await screen.findByRole('button', { name: /alice/ }))
    await waitFor(() =>
      expect(api.memoryRecords).toHaveBeenCalledWith('alice', expect.objectContaining({ org: 5 })),
    )
  })
})
