import { Link, useLocation, useParams } from 'react-router-dom'
import './WizardProgress.css'

const STEPS = [
  { stage: 'intent', label: 'Your challenge' },
  { stage: 'requirements', label: 'Requirements' },
  { stage: 'team', label: 'Meet your team' },
  { stage: 'refine', label: 'Refine' },
  { stage: 'test', label: 'Try it out' },
  { stage: 'deploy', label: 'Go live' },
]

function pathFor(stage, sessionId) {
  if (stage === 'intent') return '/wizard'
  return `/wizard/${sessionId}/${stage}`
}

// Renders the six-stage progress bar. `session` (may be null while the
// Intent stage hasn't created one yet) determines which later stages are
// reachable -- a customer can always look back, but can't skip ahead of
// what's actually been generated.
export default function WizardProgress({ session }) {
  const { sessionId } = useParams()
  const location = useLocation()

  const currentStage = location.pathname === '/wizard' ? 'intent' : location.pathname.split('/').pop()

  const unlocked = {
    intent: true,
    requirements: Boolean(sessionId),
    team: Boolean(session?.requirements_json),
    refine: Boolean(session?.specification_json),
    test: Boolean(session?.specification_json),
    deploy: Boolean(session?.specification_json),
  }

  return (
    <ol className="wizard-progress">
      {STEPS.map((step, index) => {
        const isCurrent = step.stage === currentStage
        const isReachable = unlocked[step.stage]
        const className = `wizard-step${isCurrent ? ' current' : ''}${isReachable ? '' : ' locked'}`

        return (
          <li key={step.stage} className={className}>
            {isReachable && !isCurrent ? (
              <Link to={pathFor(step.stage, sessionId)}>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{step.label}</span>
              </Link>
            ) : (
              <span>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{step.label}</span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
