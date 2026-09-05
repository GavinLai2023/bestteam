import { describe, expect, it } from 'vitest'
import { deriveWorkingAgents } from './workingAgents'
import type { TraceEvent } from './types'

const started = (agent: string, kind: 'agent' | 'subagent' = 'agent'): TraceEvent => ({
  type: 'agent_working',
  agent,
  data: { kind, state: 'started' },
})
const liveCompleted = (agent: string, kind: 'agent' | 'subagent' = 'subagent'): TraceEvent => ({
  type: 'agent_working',
  agent,
  data: { kind, state: 'completed' },
})
const persisted = (type: string, agent: string): TraceEvent => ({ type, agent, data: 'text' })

describe('deriveWorkingAgents', () => {
  it('is empty before anyone starts', () => {
    expect(deriveWorkingAgents([{ type: 'run_started' }])).toEqual({ working: [], completedAgents: 0 })
  })

  it('adds an agent on its live start and removes it on its persisted completion', () => {
    expect(deriveWorkingAgents([started('a')]).working).toEqual([{ agent: 'a', kind: 'agent' }])
    const after = deriveWorkingAgents([started('a'), persisted('agent_completed', 'a')])
    expect(after.working).toEqual([])
    expect(after.completedAgents).toBe(1)
  })

  it('keeps start order for a parallel team', () => {
    expect(deriveWorkingAgents([started('b'), started('a')]).working.map((w) => w.agent)).toEqual(['b', 'a'])
  })

  it('removes a subordinate on its live completion and keeps the manager', () => {
    const { working } = deriveWorkingAgents([
      started('manager'),
      started('researcher', 'subagent'),
      liveCompleted('researcher'),
    ])
    expect(working).toEqual([{ agent: 'manager', kind: 'agent' }])
  })

  it('also honours the persisted subagent_completed', () => {
    const { working, completedAgents } = deriveWorkingAgents([
      started('manager'),
      started('researcher', 'subagent'),
      persisted('subagent_completed', 'researcher'),
    ])
    expect(working).toEqual([{ agent: 'manager', kind: 'agent' }])
    expect(completedAgents).toBe(0)
  })

  it('ignores a duplicate start and a removal of someone not working', () => {
    expect(deriveWorkingAgents([started('a'), started('a')]).working).toHaveLength(1)
    expect(deriveWorkingAgents([persisted('agent_completed', 'zed')]).working).toEqual([])
  })

  it('clears everyone at a terminal event', () => {
    for (const type of ['run_completed', 'run_failed', 'run_cancelled']) {
      expect(deriveWorkingAgents([started('a'), started('b'), { type, data: 'x' }]).working).toEqual([])
    }
  })

  it('ignores events with no agent', () => {
    expect(deriveWorkingAgents([{ type: 'agent_working', data: { kind: 'agent', state: 'started' } }]).working).toEqual([])
  })
})
