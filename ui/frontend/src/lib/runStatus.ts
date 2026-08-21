import { useTranslation } from 'react-i18next'

// A run's `status` as the API reports it. These are wire values and are never
// translated in place -- `runs.status` is compared against stored data, so the
// same discipline applies here as to the backend's fault-classification
// strings: map at the render layer, never change what is stored or sent.
export const RUN_STATUSES = ['running', 'completed', 'failed', 'cancelled'] as const

export type RunStatus = (typeof RUN_STATUSES)[number]

// Turns a wire status into something a customer can read. The Activity and
// Trace pages previously rendered these raw, so a customer saw a lowercase
// `cancelled` badge while "My teams" -- one click away -- carefully said
// "Live" and "In Progress" (audit finding F5).
export function useRunStatusLabel() {
  const { t } = useTranslation()
  return (status: string): string => {
    switch (status) {
      case 'running':
        return t('runStatus.running')
      case 'completed':
        return t('runStatus.completed')
      case 'failed':
        return t('runStatus.failed')
      case 'cancelled':
        return t('runStatus.cancelled')
      default:
        // An unrecognised status is shown as-is rather than hidden or guessed
        // at: a status this build doesn't know about is information, and
        // swallowing it would make a new backend state invisible.
        return status
    }
  }
}
