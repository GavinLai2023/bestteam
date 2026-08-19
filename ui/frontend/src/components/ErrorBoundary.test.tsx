import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ErrorBoundary from './ErrorBoundary'

function Boom(): never {
  throw new Error('render exploded')
}

let consoleError: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  // React reports the caught error on console.error; keep test output clean.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  consoleError.mockRestore()
})

describe('ErrorBoundary', () => {
  it('renders its children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all good')).toBeInTheDocument()
  })

  it('replaces a crashed subtree with a recoverable message instead of a blank page', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('heading', { name: /something went wrong/i })).toBeInTheDocument()
    // The message tells a non-technical user what to do and what to report,
    // and never shows the raw exception text.
    expect(screen.getByText(/tell the operator the time and the page/i)).toBeInTheDocument()
    expect(screen.queryByText('render exploded')).not.toBeInTheDocument()
  })

  it('reloads the page from the fallback button', () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    fireEvent.click(screen.getByRole('button', { name: /reload/i }))
    expect(reload).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })
})
