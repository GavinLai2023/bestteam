import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ShareProgress from './ShareProgress'
import type { TraceEvent } from '../lib/types'

// What the visitor's socket actually delivers: `visitor_safe_event` strips
// every field but the type.
const completed = (count: number): TraceEvent[] =>
  Array.from({ length: count }, () => ({
    type: 'agent_completed',
    pipeline: null,
    agent: null,
    data: null,
    usage: [],
  })) as unknown as TraceEvent[]

describe('ShareProgress', () => {
  it('counts the step in progress, not just the finished ones', () => {
    render(<ShareProgress events={completed(1)} steps={3} />)
    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument()
  })

  it('starts at the first step before anything has completed', () => {
    render(<ShareProgress events={[]} steps={3} />)
    expect(screen.getByText('Step 1 of 3')).toBeInTheDocument()
  })

  it('never exceeds the denominator', () => {
    render(<ShareProgress events={completed(9)} steps={3} />)
    expect(screen.getByText('Step 3 of 3')).toBeInTheDocument()
  })

  it('lights one dot per step reached', () => {
    const { container } = render(<ShareProgress events={completed(1)} steps={3} />)
    expect(container.querySelectorAll('.share-progress-dot')).toHaveLength(3)
    expect(container.querySelectorAll('.share-progress-dot.on')).toHaveLength(2)
  })

  it('shows a pulse instead of a count when there is no honest denominator', () => {
    const { container } = render(<ShareProgress events={completed(1)} steps={null} />)
    expect(screen.queryByText(/step/i)).not.toBeInTheDocument()
    expect(container.querySelector('.share-progress-pulse')).toBeInTheDocument()
  })
})
