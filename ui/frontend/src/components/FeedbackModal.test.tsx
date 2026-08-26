import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import FeedbackModal from './FeedbackModal'

const submitButton = () => screen.getByRole('button', { name: /^send$/i })

const typeBody = (text: string) =>
  fireEvent.change(screen.getByLabelText(/feedback/i), { target: { value: text } })

beforeEach(() => {
  vi.clearAllMocks()
})

describe('FeedbackModal', () => {
  it('renders nothing while closed', () => {
    const { container } = render(
      <FeedbackModal open={false} onClose={vi.fn()} onSubmit={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('blocks a submit until something is typed', () => {
    render(<FeedbackModal open onClose={vi.fn()} onSubmit={vi.fn()} />)
    expect(submitButton()).toBeDisabled()
    typeBody('the trace page is blank')
    expect(submitButton()).toBeEnabled()
  })

  it('submits the selected kind and trimmed body, then thanks', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<FeedbackModal open onClose={vi.fn()} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByLabelText(/make a suggestion/i))
    typeBody('  add an export button  ')
    fireEvent.click(submitButton())

    await waitFor(() => expect(screen.getByText(/thank you/i)).toBeInTheDocument())
    expect(onSubmit).toHaveBeenCalledWith('suggestion', 'add an export button')
  })

  it('defect is the default kind', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<FeedbackModal open onClose={vi.fn()} onSubmit={onSubmit} />)
    typeBody('it broke')
    fireEvent.click(submitButton())
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('defect', 'it broke'))
  })

  it('a 429 shows the daily-limit message', async () => {
    const tooMany = Object.assign(new Error('cap'), { status: 429 })
    const onSubmit = vi.fn().mockRejectedValue(tooMany)
    render(<FeedbackModal open onClose={vi.fn()} onSubmit={onSubmit} />)
    typeBody('x')
    fireEvent.click(submitButton())
    await waitFor(() => expect(screen.getByText(/limit reached/i)).toBeInTheDocument())
  })

  it('any other failure keeps the draft and re-enables send', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('boom'))
    render(<FeedbackModal open onClose={vi.fn()} onSubmit={onSubmit} />)
    typeBody('my precious repro steps')
    fireEvent.click(submitButton())
    await waitFor(() => expect(screen.getByText(/couldn't send/i)).toBeInTheDocument())
    expect(screen.getByLabelText(/feedback/i)).toHaveValue('my precious repro steps')
    expect(submitButton()).toBeEnabled()
  })

  it('close resets the form', () => {
    const onClose = vi.fn()
    render(<FeedbackModal open onClose={onClose} onSubmit={vi.fn()} />)
    typeBody('half-typed')
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onClose).toHaveBeenCalled()
  })
})
