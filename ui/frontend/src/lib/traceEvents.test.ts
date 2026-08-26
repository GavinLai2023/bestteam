import { describe, expect, it } from 'vitest'
import { EVENT_LABELS, renderEventData } from './traceEvents'

// The two event types only an admin's diagnostic re-run produces (see
// core/trace.py): they need a technical label and a one-line summary like
// every other type, or AdminRunDetail falls back to the raw type string.
describe('diagnostic trace events', () => {
  it('have technical labels', () => {
    expect(EVENT_LABELS.agent_prompt).toBeDefined()
    expect(EVENT_LABELS.model_turn).toBeDefined()
  })

  it('summarises an agent_prompt by size, not by dumping the prompt', () => {
    const line = renderEventData({
      type: 'agent_prompt',
      agent: 'a',
      data: { system_prompt: 'You are a. Your goal: g', input: 'hello' },
    })
    expect(line).toBe('system prompt 23 chars · input 5 chars')
  })

  it('summarises a model_turn with its tool calls and a content excerpt', () => {
    expect(
      renderEventData({
        type: 'model_turn',
        agent: 'a',
        data: { turn: 1, content: '', tool_calls: [{ name: 'product_docs', args: { query: 'refunds' } }] },
      }),
    ).toBe('turn 1 · calls product_docs')
    expect(
      renderEventData({
        type: 'model_turn',
        agent: 'a',
        data: { turn: 2, content: 'Refunds take 14 days.', tool_calls: [] },
      }),
    ).toBe('turn 2 · Refunds take 14 days.')
    expect(
      renderEventData({
        type: 'model_turn',
        agent: 'a',
        data: { turn: 3, content: 'x'.repeat(300), tool_calls: [] },
      }),
    ).toBe(`turn 3 · ${'x'.repeat(200)}…`)
  })
})

// Grounding-lite (core/grounding.py): one event per knowledge-base agent
// turn, saying whether its [source: …] tags name passages it retrieved.
describe('grounding_checked', () => {
  it('has a technical label', () => {
    expect(EVENT_LABELS.grounding_checked).toBe('📎 grounding checked')
  })

  it('summarises the counts and lists the unverified labels', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: { searches: 1, hit_count: 3, cited: 2, verified: 1, unverified: ['handbook.pdf, p.99'] },
      }),
    ).toBe('1 search · 3 passages · 2 cited · 1 verified — unverified: handbook.pdf, p.99')
  })

  it('omits the unverified clause when every citation was verified', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: { searches: 2, hit_count: 4, cited: 2, verified: 2, unverified: [] },
      }),
    ).toBe('2 searches · 4 passages · 2 cited · 2 verified')
  })

  // grounding_policy (retry/refuse): the event says when the policy acted.
  it('notes a corrective retry', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: {
          searches: 1, hit_count: 3, cited: 1, verified: 1, unverified: [],
          policy: 'retry', retried: true, refused: false,
        },
      }),
    ).toBe('1 search · 3 passages · 1 cited · 1 verified — retried')
  })

  it('notes a refused answer', () => {
    expect(
      renderEventData({
        type: 'grounding_checked',
        agent: 'a',
        data: {
          searches: 1, hit_count: 0, cited: 0, verified: 0, unverified: [],
          policy: 'refuse', retried: true, refused: true,
        },
      }),
    ).toBe('1 search · 0 passages · 0 cited · 0 verified — retried — answer refused')
  })
})
