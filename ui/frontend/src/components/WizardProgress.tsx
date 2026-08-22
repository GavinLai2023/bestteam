import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams } from 'react-router-dom'
import type { BuilderSession } from '../lib/types'
import './WizardProgress.css'

interface WizardProgressProps {
  session: BuilderSession | null
}

// Stage order only. The labels live in the locale files and are
// resolved per render -- a module-level t() would be evaluated once,
// before i18n is ready, and would never follow a language switch.
const STEPS = ['intent', 'documents', 'preview', 'confirm', 'deploy'] as const

type Step = (typeof STEPS)[number]

function pathFor(stage: string, sessionId: string | undefined) {
  if (stage === 'intent') return '/wizard'
  return `/wizard/${sessionId}/${stage}`
}

// Renders the five-step progress bar (`STEPS` above). `session` (may be null
// while the Intent stage hasn't created one yet) determines which later stages
// are reachable -- a customer can always look back, but can't skip ahead of
// what's actually been generated.
export default function WizardProgress({ session }: WizardProgressProps) {
  const { t } = useTranslation()
  const { sessionId } = useParams()
  const location = useLocation()

  const currentStage = location.pathname === '/wizard' ? 'intent' : location.pathname.split('/').pop()

  const labelFor = (stage: Step) => {
    switch (stage) {
      case 'intent':
        return t('wizard.steps.intent')
      case 'documents':
        return t('wizard.steps.documents')
      case 'preview':
        return t('wizard.steps.preview')
      case 'confirm':
        return t('wizard.steps.confirm')
      case 'deploy':
        return t('wizard.steps.deploy')
    }
  }

  const unlocked: Record<string, boolean> = {
    intent: true,
    documents: Boolean(session),
    preview: Boolean(session?.specification_json),
    confirm: Boolean(session?.specification_json),
    deploy: Boolean(session?.specification_json),
  }

  return (
    <ol className="wizard-progress">
      {STEPS.map((stage, index) => {
        const isCurrent = stage === currentStage
        const isReachable = unlocked[stage]
        const className = `wizard-step${isCurrent ? ' current' : ''}${isReachable ? '' : ' locked'}`
        const label = labelFor(stage)

        return (
          <li key={stage} className={className}>
            {isReachable && !isCurrent ? (
              <Link to={pathFor(stage, sessionId)}>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{label}</span>
              </Link>
            ) : (
              <span>
                <span className="wizard-step-number">{index + 1}</span>
                <span className="wizard-step-label">{label}</span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
