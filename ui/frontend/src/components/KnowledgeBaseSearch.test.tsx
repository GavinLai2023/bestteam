import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import KnowledgeBaseSearch from './KnowledgeBaseSearch'
import { api } from '../lib/api'
import type { KnowledgeBaseSearchResponse } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: {
    searchOwnKnowledgeBase: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const response = (
  overrides: Partial<KnowledgeBaseSearchResponse> = {},
): KnowledgeBaseSearchResponse => ({
  query: 'refund policy',
  hit_count: 1,
  results: [
    {
      citation: 'handbook.pdf, p.3 § Refunds',
      source: 'handbook.pdf',
      page: 3,
      heading: 'Refunds',
      text: 'Returns are accepted within 30 days.',
    },
  ],
  ...overrides,
})

async function submit(query = 'refund policy') {
  fireEvent.change(screen.getByLabelText(/search/i), { target: { value: query } })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }))
  })
}

describe('KnowledgeBaseSearch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.searchOwnKnowledgeBase.mockResolvedValue(response())
  })

  it('submits the query and renders each passage under its citation', async () => {
    render(<KnowledgeBaseSearch name="policies" />)
    await submit()

    expect(mockedApi.searchOwnKnowledgeBase).toHaveBeenCalledWith('policies', 'refund policy')
    expect(await screen.findByText('handbook.pdf, p.3 § Refunds')).toBeInTheDocument()
    expect(screen.getByText(/Returns are accepted within 30 days/)).toBeInTheDocument()
  })

  it('says so when nothing matched, rather than showing an empty list', async () => {
    mockedApi.searchOwnKnowledgeBase.mockResolvedValue(response({ hit_count: 0, results: [] }))
    render(<KnowledgeBaseSearch name="policies" />)
    await submit('nothing in here')

    expect(await screen.findByText('No matching passages.')).toBeInTheDocument()
  })

  it("shows the backend's own refusal inline", async () => {
    mockedApi.searchOwnKnowledgeBase.mockRejectedValue(
      new Error("Knowledge base 'policies' has no completed ingestion yet."),
    )
    render(<KnowledgeBaseSearch name="policies" />)
    await submit()

    expect(await screen.findByText(/no completed ingestion yet/)).toBeInTheDocument()
  })

  it('shows a plain-language preview for an XML chunk, with the raw markup collapsed', async () => {
    mockedApi.searchOwnKnowledgeBase.mockResolvedValue(
      response({
        results: [
          {
            citation: 'diagram.xml § bpmn:process',
            source: 'diagram.xml',
            page: null,
            heading: 'bpmn:process',
            text: '<bpmn:userTask id="Activity_1" name="The employee is based at Capalaba Library">',
          },
        ],
      }),
    )
    render(<KnowledgeBaseSearch name="policies" />)
    await submit()

    expect(await screen.findByText('The employee is based at Capalaba Library')).toBeInTheDocument()
    expect(screen.queryByText(/bpmn:userTask/)).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /show original text/i }))
    })
    expect(screen.getByText(/<bpmn:userTask/)).toBeInTheDocument()
  })

  it('disables the button while a search is in flight, so one click is one search', async () => {
    let resolve: (value: KnowledgeBaseSearchResponse) => void = () => {}
    mockedApi.searchOwnKnowledgeBase.mockReturnValue(
      new Promise<KnowledgeBaseSearchResponse>((r) => {
        resolve = r
      }),
    )
    render(<KnowledgeBaseSearch name="policies" />)
    await submit()

    const button = screen.getByRole('button', { name: /searching/i })
    expect(button).toBeDisabled()

    await act(async () => {
      resolve(response())
    })
    await waitFor(() => expect(screen.getByRole('button', { name: /^search$/i })).toBeEnabled())
    expect(mockedApi.searchOwnKnowledgeBase).toHaveBeenCalledTimes(1)
  })
})
