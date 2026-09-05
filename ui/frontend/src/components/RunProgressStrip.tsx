import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { WorkingAgent } from '../lib/workingAgents'
import './RunProgressStrip.css'

interface RunProgressStripProps {
  working: WorkingAgent[]
  completedAgents: number
  // The team's size when the page knows it; undefined drops the "of N".
  agentCount?: number
  displayNameFor: (agentName: string) => string
}

// The live milestone (spec 2026-09-05): who is working right now, for how
// long, and -- when the team's size is known -- how far along the team is.
// Rendering is keyed on what the events say, not on a team mode the page
// does not have: several agents at once is a parallel team, a subordinate
// present is a delegation.
export default function RunProgressStrip({ working, completedAgents, agentCount, displayNameFor }: RunProgressStripProps) {
  const { t } = useTranslation()
  // When the current stretch of work began. Reset whenever the set of
  // working agents changes, so "agent 2 of 6 · 3s" counts agent 2's own
  // time; after a reconnect it restarts, which the spec accepts. `Date.now()`
  // is only ever read from this effect/timer, never during render, which is
  // what keeps react-hooks/purity happy about it.
  const key = working.map((w) => w.agent).join('|')
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    const since = Date.now()
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the counter restarts with each stretch of work
    setSeconds(0)
    if (!key) return undefined
    const id = setInterval(() => setSeconds(Math.max(0, Math.floor((Date.now() - since) / 1000))), 1000)
    return () => clearInterval(id)
  }, [key])

  if (working.length === 0) return null

  const topLevel = working.filter((w) => w.kind === 'agent')
  const subordinate = working.find((w) => w.kind === 'subagent')
  let text: string
  if (subordinate) {
    const manager = topLevel[0]?.agent ?? subordinate.agent
    text = t('run.progressDelegated', {
      manager: displayNameFor(manager),
      agent: displayNameFor(subordinate.agent),
      seconds,
    })
  } else if (topLevel.length > 1) {
    text = agentCount
      ? t('run.progressParallelOfN', { count: topLevel.length, done: completedAgents, total: agentCount, seconds })
      : t('run.progressParallel', { count: topLevel.length, seconds })
  } else {
    const name = displayNameFor(topLevel[0].agent)
    text = agentCount
      ? t('run.progressOneOfN', { name, index: completedAgents + 1, total: agentCount, seconds })
      : t('run.progressOne', { name, seconds })
  }
  return (
    <p className="run-progress-strip" role="status">
      {text}
    </p>
  )
}
