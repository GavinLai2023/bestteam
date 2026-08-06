import { Link, useLocation, useParams } from 'react-router-dom'
import type { BuilderSession } from '../lib/types'
import './WizardProgress.css'

interface WizardProgressProps {
  session: BuilderSession | null
}

const STEPS = [
  { stage: 'intent', label: 'Your challenge' },
  { stage: 'preview', label: 'Meet your team' },
  { stage: 'confirm', label: 'Confirm' },
  { stage: 'deploy', label: 'Go live' },
]

function pathFor(stage: string, sessionId: string | undefined) {
  if (stage === 'intent') return '/wizard'
  return `/wizard/${sessionId}/${stage}`
}

// Renders the four-stage progress bar. `session` (may be null while the
// Intent stage hasn't created one yet) determines which later stages are
// reachable -- a customer can always look back, but can't skip ahead of
// what's actually been generated.
export default function WizardProgress({ session }: WizardProgressProps) {
  const { sessionId } = useParams()
  const location = useLocation()

  const currentStage = location.pathname === '/wizard' ? 'intent' : location.pathname.split('/').pop()

  const unlocked: Record<string, boolean> = {
    intent: true,
    preview: Boolean(session?.specification_json),
    confirm: Boolean(session?.specification_json),
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
