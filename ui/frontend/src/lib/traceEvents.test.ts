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
