import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EVENT_LABELS, renderEventData, useDetailedEventLine } from './traceEvents'
import type { TraceEvent } from './types'

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

// The customer's expanded run view. It narrates the same event stream as the
// admin trace, but says what the team did rather than what the engine did:
// no tool identifiers, no durations, no iteration counters, and none of the
// platform's own machinery (memory, grounding).
describe('useDetailedEventLine', () => {
  const lineFor = (event: TraceEvent, displayNames: Record<string, string> = {}) => {
    const { result } = renderHook(() => useDetailedEventLine((name) => displayNames[name] ?? name))
    return result.current(event)
  }

  it('hides the platform machinery a customer has no use for', () => {
    for (const type of [
      'tool_started',
      'agent_progress',
      'subagent_started',
      'subagent_completed',
      'memory_recalled',
      'memory_recorded',
      'memory_failed',
      'grounding_checked',
      'agent_prompt',
      'model_turn',
    ]) {
      expect(lineFor({ type, agent: 'a', data: { note: 'iteration 2 of 8' } })).toBeNull()
    }
  })

  it('names an agent by the name the wizard gave it, and shows its role', () => {
    const line = lineFor(
      { type: 'agent_started', agent: 'triage_agent', data: { role: 'Intake Officer', goal: 'g' } },
      { triage_agent: 'Triage Assistant' },
    )
    expect(line?.title).toBe('Triage Assistant got started')
    expect(line?.detail).toBe('Intake Officer')
  })

  it('falls back to the technical name when a team predates friendly names', () => {
    expect(lineFor({ type: 'agent_completed', agent: 'triage_agent', data: 'out' })?.title).toBe(
      'triage_agent finished their part',
    )
  })

  it('reads a delegation as one team member handing work to another', () => {
    const names = { manager: 'Team Lead', reviewer: 'Reviewer' }
    const started = lineFor(
      { type: 'delegation_started', agent: 'manager', data: { to: 'reviewer', task_summary: 'Check this draft' } },
      names,
    )
    expect(started?.title).toBe('Team Lead asked Reviewer to help')
    expect(started?.detail).toBe('Check this draft')

    const done = lineFor(
      { type: 'delegation_completed', agent: 'manager', data: { to: 'reviewer', summary: 's' } },
      names,
    )
    expect(done?.title).toBe('Team Lead got an answer back from Reviewer')
    expect(done?.detail).toBeUndefined()
  })

  it("hides a delegation's own tool call, which the delegation lines already tell", () => {
    // The manager calls the subordinate through a generated `delegate_to_x`
    // tool (langgraph_adapter._delegation_tools), so every delegation also
    // emits a tool_completed naming it: a platform-internal identifier, and a
    // duplicate of the two lines above.
    expect(
      lineFor({ type: 'tool_completed', agent: 'manager', data: { tool: 'delegate_to_reviewer', success: true } }),
    ).toBeNull()
  })

  it('names a knowledge base and how much it found, not the search internals', () => {
    const line = lineFor({
      type: 'tool_completed',
      agent: 'a',
      data: { tool: 'Contract files', success: true, duration_ms: 340, hit_count: 4, query: 'notice period' },
    })
    expect(line?.title).toBe('Looked through “Contract files” and found 4 passages')
    expect(line?.detail).toBeUndefined()
  })

  it('says a knowledge base found nothing rather than reporting zero passages', () => {
    expect(
      lineFor({
        type: 'tool_completed',
        agent: 'a',
        data: { tool: 'Contract files', success: true, duration_ms: 12, hit_count: 0, query: 'q' },
      })?.title,
    ).toBe('Looked through “Contract files” and found nothing relevant')
  })

  it('describes a built-in tool in plain words, with no identifier or timing', () => {
    const line = lineFor({
      type: 'tool_completed',
      agent: 'a',
      data: { tool: 'email_draft_reply', success: true, duration_ms: 820, summary: 'Draft created.' },
    })
    expect(line?.title).toBe('Drafted a reply')
    expect(line?.title).not.toContain('email_draft_reply')
    expect(line?.title).not.toContain('820')
  })

  it('says plainly when a step did not succeed', () => {
    expect(
      lineFor({ type: 'tool_completed', agent: 'a', data: { tool: 'email_read', success: false } })?.title,
    ).toBe('Tried to read the message, without success')
  })

  it('names a custom tool, since that name is the customer’s own', () => {
    expect(
      lineFor({ type: 'tool_completed', agent: 'a', data: { tool: 'lookup_tenancy', success: true } })?.title,
    ).toBe('Used “lookup_tenancy”')
  })

  it('keeps the run lifecycle in the same words the collapsed view uses', () => {
    expect(lineFor({ type: 'run_started' })?.title).toBe('Your team got started')
    expect(lineFor({ type: 'run_completed', data: 'out' })?.title).toBe('All done!')
    expect(lineFor({ type: 'run_failed', data: 'boom' })?.title).toBe('Something went wrong')
  })
})
