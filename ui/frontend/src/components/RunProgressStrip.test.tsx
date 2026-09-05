import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import RunProgressStrip from './RunProgressStrip'

const friendly = (name: string) => ({ a: 'Ada', b: 'Bert', manager: 'Lead', researcher: 'Scout' })[name] ?? name

describe('RunProgressStrip', () => {
  it('renders nothing when nobody is working', () => {
    const { container } = render(
      <RunProgressStrip working={[]} completedAgents={0} agentCount={3} displayNameFor={friendly} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('names the one working agent with its position when the team size is known', () => {
    render(
      <RunProgressStrip
        working={[{ agent: 'b', kind: 'agent' }]}
        completedAgents={1}
        agentCount={3}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Bert is working · agent 2 of 3 · 0s')
  })

  it('drops the position when the team size is unknown', () => {
    render(
      <RunProgressStrip working={[{ agent: 'a', kind: 'agent' }]} completedAgents={0} displayNameFor={friendly} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Ada is working · 0s')
  })

  it('counts members for a parallel team', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'a', kind: 'agent' },
          { agent: 'b', kind: 'agent' },
        ]}
        completedAgents={1}
        agentCount={4}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('2 members working at once · 1 of 4 done · 0s')
  })

  it('narrates a delegation without a position', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'manager', kind: 'agent' },
          { agent: 'researcher', kind: 'subagent' },
        ]}
        completedAgents={0}
        agentCount={2}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Lead is working · handed to Scout · 0s')
  })

  it('never shows a technical name it was given a friendly one for', () => {
    render(
      <RunProgressStrip working={[{ agent: 'a', kind: 'agent' }]} completedAgents={0} displayNameFor={friendly} />,
    )
    expect(screen.getByRole('status')).not.toHaveTextContent(/\ba\b is working/)
  })

  it('counts members for a parallel team with no known team size', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'a', kind: 'agent' },
          { agent: 'b', kind: 'agent' },
        ]}
        completedAgents={0}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('2 members working at once · 0s')
  })

  it('clamps the position at the team size when completedAgents overshoots it', () => {
    // completedAgents can exceed agentCount (see the clamp's own comment in
    // RunProgressStrip.tsx) -- the line must sit at the last position,
    // never claim "agent 4 of 3".
    render(
      <RunProgressStrip
        working={[{ agent: 'b', kind: 'agent' }]}
        completedAgents={5}
        agentCount={3}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Bert is working · agent 3 of 3 · 0s')
  })

  it('clamps "done" at the team size for a parallel team too', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'a', kind: 'agent' },
          { agent: 'b', kind: 'agent' },
        ]}
        completedAgents={5}
        agentCount={3}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('2 members working at once · 3 of 3 done · 0s')
  })

  it('keeps the elapsed time out of the announced sentence', () => {
    render(
      <RunProgressStrip working={[{ agent: 'a', kind: 'agent' }]} completedAgents={0} displayNameFor={friendly} />,
    )
    const status = screen.getByRole('status')
    const elapsed = status.querySelector('[aria-hidden="true"]')
    expect(elapsed).not.toBeNull()
    expect(elapsed).toHaveTextContent('0s')
    // The sentence itself (the status element minus the hidden span) carries
    // no aria-hidden marker -- only the ticking part is silenced.
    expect(status).not.toHaveAttribute('aria-hidden')
  })
})
