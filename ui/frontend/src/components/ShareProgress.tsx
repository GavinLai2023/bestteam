import { useTranslation } from 'react-i18next'
import type { TraceEvent } from '../lib/types'
import './ShareProgress.css'

interface Props {
  events: TraceEvent[]
  steps: number | null
}

// How far through the team this turn is. Anonymous by construction: the
// visitor sees a position, never a name, a role or a model -- `agent_completed`
// reaches this page type-only through `visitor_safe_event`, so counting those
// events is the most this surface can honestly say.
//
// `steps` is null for a hierarchical team, whose manager emits one completion
// however many subordinates it delegates to; there the pulse says "working"
// without pretending to know how much is left.
export default function ShareProgress({ events, steps }: Props) {
  const { t } = useTranslation()

  if (steps === null || steps <= 0) {
    return <span className="share-progress-pulse" aria-hidden="true" />
  }

  const done = events.filter((event) => event.type === 'agent_completed').length
  // The step in progress counts, and the count is clamped: a team can emit
  // more completions than the denominator anticipated (a hierarchical team
  // nested behind a sequential one, say), and "Step 4 of 3" is worse than
  // sitting at the last dot.
  const current = Math.min(done + 1, steps)

  return (
    <span className="share-progress">
      <span className="share-progress-dots" aria-hidden="true">
        {Array.from({ length: steps }, (_, index) => (
          <span key={index} className={index < current ? 'share-progress-dot on' : 'share-progress-dot'} />
        ))}
      </span>
      <span className="share-progress-label">{t('share.stepProgress', { n: current, total: steps })}</span>
    </span>
  )
}
