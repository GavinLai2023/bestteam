import { useTranslation } from 'react-i18next'
import { Fragment } from 'react'
import EmployeeCard from './EmployeeCard'
import type { Specification, TeamMode } from '../lib/types'

interface TeamFlowProps {
  specification?: Specification | null
}



// Renders a customer-friendly "how your team works together" diagram from a
// Specification: one block per team (in pipeline-step order), showing the
// manager (for hierarchical teams) and the agents who do the work, with no
// technical jargon -- just job titles and one-line descriptions.
export default function TeamFlow({ specification }: TeamFlowProps) {
  const { t } = useTranslation()

  // An unrecognised mode renders as-is rather than vanishing, matching
  // lib/runStatus.ts's rule for an unknown run status.
  const modeLabel = (mode: TeamMode) => {
    switch (mode) {
      case 'sequential':
        return t('wizard.teamMode.sequential')
      case 'parallel':
        return t('wizard.teamMode.parallel')
      case 'hierarchical':
        return t('wizard.teamMode.hierarchical')
      default:
        return mode
    }
  }

  if (!specification) return null

  const agentsByName = Object.fromEntries((specification.agents ?? []).map((agent) => [agent.name, agent]))
  const teamsByName = Object.fromEntries((specification.teams ?? []).map((team) => [team.name, team]))
  const steps = specification.pipeline?.steps ?? []

  return (
    <div className="team-flow">
      {steps.map((stepName, index) => {
        const team = teamsByName[stepName]
        if (!team) return null

        const memberNames = team.manager ? team.agents.filter((name) => name !== team.manager) : team.agents

        return (
          <div key={stepName}>
            {index > 0 && <div className="team-flow-arrow">↓</div>}
            <div className="team-block">
              <div className="team-block-header">
                <h3>{team.display_name || team.name}</h3>
                <span className="team-mode-badge">{modeLabel(team.mode)}</span>
              </div>
              {team.friendly_description && <p className="team-block-description">{team.friendly_description}</p>}

              {team.manager && (
                <>
                  <div className="team-manager-row">
                    <EmployeeCard agent={agentsByName[team.manager]} />
                  </div>
                  {memberNames.length > 0 && <div className="team-flow-arrow">↓</div>}
                </>
              )}

              <div className={`team-members-row ${team.mode === 'sequential' ? 'sequential' : ''}`}>
                {memberNames.map((name, i) => (
                  <Fragment key={name}>
                    {team.mode === 'sequential' && i > 0 && <span className="sequential-arrow">→</span>}
                    <EmployeeCard agent={agentsByName[name]} />
                  </Fragment>
                ))}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
